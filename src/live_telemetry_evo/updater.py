"""Async update check against GitHub releases.

On startup the app queries the GitHub releases page for
``SkyVVriter/live-telemetry-evo`` and, if the tag is newer than the
running version, downloads the matching ``LiveTelemetryEvo-<version>.exe``
asset into the same folder as the running executable. The downloaded
file's SHA256 hash is verified against ``SHA256SUMS.txt`` from the same
release; the file is rejected on mismatch. The user sees a system-tray
balloon when the download finishes; failures are silent.

Re-downloads are skipped — if a file with the asset's filename already
exists next to the .exe (from a previous run), we leave it alone.

The tray menu has a "Check for Updates" action whose label tracks the
controller's state machine — Checking… / Downloading… / Restart to
Update — and offers a manual re-check from the IDLE state. The
restart path launches the downloaded .exe detached, then quits the
current process so the new build can attach to the same SHM tags.

Release channels
~~~~~~~~~~~~~~~~
Setting ``release_channel`` in ``positions.json`` (via settings helpers)
controls which releases the updater considers:

* ``"prod"`` (default) — only full releases (no pre-release suffix).
* ``"test"`` — pre-releases whose tag matches ``v*-test*``
  (e.g. ``v0.9.0-test.3``). When on the test channel the updater picks
  the highest version including semver pre-release ordering.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl  # imported eagerly so PyInstaller bundles _ssl + OpenSSL DLLs
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QApplication

from .logbook import log


GITHUB_RELEASES_URL = "https://github.com/SkyVVriter/live-telemetry-evo/releases"
HTTP_TIMEOUT_S = 10
USER_AGENT = "live-telemetry-evo-updater"


class SemVer:
    """Semantic version with optional pre-release component for proper
    ordering per the semver spec: release > pre-release, and pre-release
    identifiers are compared numerically when both are digits."""

    __slots__ = ("major", "minor", "patch", "pre")

    def __init__(self, major: int, minor: int, patch: int,
                 pre: tuple[str, ...] = ()) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre

    def _cmp_key(self):
        if not self.pre:
            return (self.major, self.minor, self.patch, 1, ())
        converted = []
        for part in self.pre:
            if part.isdigit():
                converted.append((0, int(part)))
            else:
                converted.append((1, part))
        return (self.major, self.minor, self.patch, 0, tuple(converted))

    def __eq__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() == other._cmp_key()

    def __lt__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() < other._cmp_key()

    def __le__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() <= other._cmp_key()

    def __gt__(self, other):
        return not self.__le__(other)

    def __ge__(self, other):
        return not self.__lt__(other)

    def __repr__(self):
        pre = f"-{'.'.join(self.pre)}" if self.pre else ""
        return f"SemVer({self.major}.{self.minor}.{self.patch}{pre})"


def parse_semver(s: str) -> Optional[SemVer]:
    """Parse ``vX.Y.Z`` or ``vX.Y.Z-pre.release.ids`` into a :class:`SemVer`.
    Returns ``None`` on unparseable input."""
    s = s.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z0-9.]+))?", s)
    if not m:
        return None
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    return SemVer(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def _parse_version(s: str) -> Optional[tuple[int, int, int]]:
    """Backward-compat helper: strip leading ``v`` and parse ``X.Y.Z``."""
    sv = parse_semver(s)
    if sv is None:
        return None
    return (sv.major, sv.minor, sv.patch)


def get_current_version() -> Optional[str]:
    """Resolve the running app's version. Frozen build: from the .exe
    filename (``LiveTelemetryEvo-X.Y.Z.exe``). Dev: from
    ``pyproject.toml`` walking up from this file. ``None`` if neither
    branch produces a match — the update check is then skipped."""
    if getattr(sys, "frozen", False):
        m = re.search(r"-(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?)$",
                       Path(sys.executable).stem)
        return m.group(1) if m else None
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            m = re.search(r'^\s*version\s*=\s*"([^"]+)"',
                          text, flags=re.MULTILINE)
            return m.group(1) if m else None
    return None


def install_dir() -> Path:
    """Folder the new .exe will be written into — alongside the running
    executable when frozen, current working directory in dev."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _is_test_tag(tag: str) -> bool:
    """True when ``tag`` looks like a test/pre-release tag (contains ``-test``)."""
    return "-test" in tag.lower()


def choose_release(releases: list[dict], channel: str,
                   current: SemVer) -> Optional[dict]:
    """Pick the best release from a list, respecting the channel.

    *releases* is a list of release dicts as returned by the GitHub releases
    page (each must carry ``tag_name``).

    * ``channel="prod"`` — only releases whose tag has NO pre-release suffix.
    * ``channel="test"`` — only releases whose tag contains ``-test``.

    Returns the release dict with the highest version that is strictly newer
    than *current*, or ``None``.
    """
    best: Optional[SemVer] = None
    best_rel: Optional[dict] = None
    for rel in releases:
        tag = (rel.get("tag_name") or "").strip()
        sv = parse_semver(tag)
        if sv is None:
            continue
        if channel == "test":
            if not _is_test_tag(tag):
                continue
        else:
            if sv.pre:
                continue
        if sv <= current:
            continue
        if best is None or sv > best:
            best = sv
            best_rel = rel
    return best_rel


def verify_sha256(file_path: Path, expected_hash: str) -> bool:
    """Return True when ``file_path``'s SHA-256 matches ``expected_hash``
    (hex, case-insensitive)."""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected_hash.strip().lower()


def _parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS.txt`` file. Returns ``{filename: hex_hash}``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0].strip()
    return out


class UpdateChecker(QObject):
    """Single-shot worker: query GitHub, compare versions, download the
    asset if newer. Lives on its own QThread; reports outcomes via
    Qt signals that are queued back to the UI thread automatically."""

    download_started = Signal(str)
    download_finished = Signal(str, str)
    already_present = Signal(str, str)
    up_to_date = Signal(str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, current_version: str,
                 channel: str = "prod",
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current = current_version
        self._channel = channel

    def run(self) -> None:
        try:
            self._run()
        finally:
            self.done.emit()

    def _run(self) -> None:
        current_sv = parse_semver(self._current)
        if current_sv is None:
            self.failed.emit(f"could not parse current version: {self._current!r}")
            return

        try:
            releases = self._fetch_releases()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.failed.emit(f"could not reach GitHub: {e}")
            return
        except ValueError as e:
            self.failed.emit(f"could not parse GitHub response: {e}")
            return

        chosen = choose_release(releases, self._channel, current_sv)
        if chosen is None:
            self.up_to_date.emit(self._current)
            return

        tag = (chosen.get("tag_name") or "").strip()
        asset_url = self._pick_asset(chosen, tag)
        if asset_url is None:
            self.failed.emit(f"no .exe asset found in release {tag}")
            return

        target = install_dir() / Path(asset_url).name
        if target.exists():
            self.already_present.emit(tag, str(target))
            return

        self.download_started.emit(tag)
        try:
            self._download(asset_url, target)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.failed.emit(f"download failed: {e}")
            return

        sha_ok = self._verify_sha(chosen, tag, target)
        if not sha_ok:
            try:
                target.unlink()
            except OSError:
                pass
            self.failed.emit(f"SHA256 mismatch for {target.name} — file deleted")
            return

        self.download_finished.emit(tag, str(target))

    @staticmethod
    def _fetch_releases() -> list[dict]:
        """Fetch the first page of releases (enough for channel selection)."""
        url = f"{GITHUB_RELEASES_URL}?per_page=30"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S,
                                     context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            # /releases/latest returns a single dict; wrap it
            return [data]
        return data if isinstance(data, list) else []

    @staticmethod
    def _fetch_text(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S,
                                     context=ctx) as resp:
            return resp.read().decode("utf-8")

    @staticmethod
    def _pick_asset(payload: dict, tag: str) -> Optional[str]:
        version = tag.lstrip("vV")
        assets = payload.get("assets") or []
        # If no assets array (non-api endpoint), synthesize one
        if not assets and version:
            name = f"LiveTelemetryEvo-{version}.exe"
            return f"{GITHUB_RELEASES_URL}/download/{tag}/{name}"
        for a in assets:
            name = (a.get("name") or "")
            if name.lower().endswith(".exe") and version in name:
                return a.get("browser_download_url")
        for a in assets:
            name = (a.get("name") or "")
            if name.lower().endswith(".exe"):
                return a.get("browser_download_url")
        return None

    @staticmethod
    def _download(url: str, target: Path) -> None:
        tmp = target.with_suffix(target.suffix + ".partial")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S,
                                     context=ctx) as resp:
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        tmp.replace(target)

    @staticmethod
    def _verify_sha(release: dict, tag: str, exe_path: Path) -> bool:
        """Download SHA256SUMS.txt from the release and verify ``exe_path``.
        Returns True if hash matches, or if the sums file is absent (older
        releases without checksums are accepted)."""
        sha_url = f"{GITHUB_RELEASES_URL}/download/{tag}/SHA256SUMS.txt"
        try:
            text = UpdateChecker._fetch_text(sha_url)
        except (urllib.error.URLError, TimeoutError, OSError):
            log("[updater] SHA256SUMS.txt not found in release; skipping verification")
            return True
        sums = _parse_sha256sums(text)
        expected = sums.get(exe_path.name)
        if expected is None:
            log(f"[updater] {exe_path.name} not listed in SHA256SUMS.txt; skipping")
            return True
        ok = verify_sha256(exe_path, expected)
        if not ok:
            log(f"[updater] SHA256 MISMATCH for {exe_path.name}")
        return ok


class UpdateController(QObject):
    """Stateful façade around :class:`UpdateChecker` for the UI.

    Drives a four-state machine the tray menu reflects directly:

    * ``IDLE`` — "Check for Updates" (clickable).
    * ``CHECKING`` — "Checking updates…" (disabled).
    * ``DOWNLOADING`` — "Downloading update…" (disabled).
    * ``READY`` — "Restart to Update" (clickable; launches the new .exe).

    Clicking the menu in ``IDLE`` triggers :meth:`start_check`. Clicking
    in ``READY`` triggers :meth:`restart_into_update`. Failures and
    "already up to date" both fall back to ``IDLE`` so the user can
    re-run the check manually.

    :class:`UpdateChecker.already_present` (the asset for a newer tag
    is already on disk from a prior session) is treated as ``READY``
    too — the user should still be able to restart into the file that
    was downloaded last time but never used.
    """

    IDLE = "idle"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    READY = "ready"

    # Emitted on every state transition so the tray menu can refresh
    # its label / enabled state without polling.
    state_changed = Signal(str, str)              # state, detail

    # Emitted when a download just completed (NOT on already_present);
    # the app uses this to fire the tray balloon notification — we
    # don't notify on already_present because that fires on every
    # startup once the file is on disk, which would be spammy.
    download_finished = Signal(str, str)          # tag, absolute path

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = self.IDLE
        self._thread: QThread | None = None
        self._checker: UpdateChecker | None = None
        self._restart_path: str | None = None
        self._current = get_current_version()

    @property
    def state(self) -> str:
        return self._state

    @property
    def restart_path(self) -> str | None:
        return self._restart_path

    def start_check(self) -> None:
        """Kick off a check on a worker thread. No-op if a check or
        download is already in flight, or if we're already pending a
        restart."""
        if self._state != self.IDLE:
            return
        if not self._current:
            log("[updater] skipped: could not determine current version")
            return

        self._set_state(self.CHECKING)

        from .settings import load_release_channel  # avoid circular
        channel = load_release_channel()

        thread = QThread(self)
        checker = UpdateChecker(self._current, channel=channel)
        checker.moveToThread(thread)
        # pylint: disable=no-member  # Qt signals are runtime metaclass magic
        thread.started.connect(checker.run)
        checker.download_started.connect(self._on_download_started)
        checker.download_finished.connect(self._on_download_finished)
        checker.already_present.connect(self._on_already_present)
        checker.up_to_date.connect(self._on_up_to_date)
        checker.failed.connect(self._on_failed)
        checker.done.connect(thread.quit)
        thread.finished.connect(checker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_thread_finished)

        self._thread = thread
        self._checker = checker
        thread.start()

    def restart_into_update(self) -> bool:
        """Launch the downloaded .exe in a detached process and quit
        the current app. Returns ``False`` if no update is ready or the
        launch failed (in which case the state stays at ``READY`` so
        the user can try again)."""
        if self._state != self.READY or not self._restart_path:
            return False
        path = Path(self._restart_path)
        if not path.exists():
            # File was deleted between download and click — degrade
            # gracefully by reverting to IDLE so a fresh check can
            # re-download it.
            log(f"[updater] restart target missing: {path}")
            self._restart_path = None
            self._set_state(self.IDLE, "restart target missing")
            return False

        flags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: the child
            # survives our exit and has no console attachment (matches
            # how the .exe is launched from Explorer).
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            # pylint: disable-next=consider-using-with
            subprocess.Popen(
                [str(path)],
                cwd=str(path.parent),
                close_fds=True,
                creationflags=flags,
            )
        except OSError as e:
            log(f"[updater] failed to launch {path}: {e}")
            return False

        log(f"[updater] launched {path} — quitting current app")
        app = QApplication.instance()
        if app is not None:
            app.quit()
        return True

    def _set_state(self, new_state: str, detail: str = "") -> None:
        if new_state == self._state:
            return
        self._state = new_state
        self.state_changed.emit(new_state, detail)

    def _on_download_started(self, tag: str) -> None:
        log(f"[updater] new release {tag} — downloading")
        self._set_state(self.DOWNLOADING, tag)

    def _on_download_finished(self, tag: str, path: str) -> None:
        log(f"[updater] downloaded {tag} -> {path}")
        self._restart_path = path
        self._set_state(self.READY, tag)
        self.download_finished.emit(tag, path)

    def _on_already_present(self, tag: str, path: str) -> None:
        log(f"[updater] {tag} already on disk at {path}")
        self._restart_path = path
        self._set_state(self.READY, tag)

    def _on_up_to_date(self, tag: str) -> None:
        log(f"[updater] up to date (latest={tag}, current={self._current})")
        self._set_state(self.IDLE, tag)

    def _on_failed(self, reason: str) -> None:
        log(f"[updater] check failed: {reason}")
        # Failure always lands back in IDLE so the user can re-trigger
        # — including failures during the DOWNLOADING phase.
        self._set_state(self.IDLE, reason)

    def _on_thread_finished(self) -> None:
        # Drop strong refs so the next start_check builds a fresh
        # worker + thread; the old ones are scheduled for deletion via
        # the deleteLater chain set up in start_check.
        self._thread = None
        self._checker = None
