"""Timestamped console + file logging for the overlay.

Every line the app emits goes through :func:`log` (replacing bare
``print``): it is printed and appended to ``logs/session.log``, both
prefixed with a bracketed local timestamp. :func:`log_action` is a thin
wrapper that tags user actions with ``[action]`` so they stand out in the
same stream — menu clicks, hotkeys, widget drags, show/hide toggles, VR
tweaks — next to the ``[overlay]`` / ``[updater]`` / source diagnostics.

Separate from the telemetry :class:`~live_telemetry_evo.logger.CsvLogger`,
which captures the car's data. One file for the whole run, appended across
runs (each run writes a ``session started`` marker), opened lazily on the
first message and line-buffered so nothing is lost on a hard kill.

Best-effort: logging must never raise into the caller, so all file I/O is
guarded. The lock serialises writes from the UI thread and the source /
VR worker threads.

If the primary ``logs/`` directory (next to the exe) is not writable
(e.g. ``C:\\Program Files\\``, UAC-protected), falls back to
``%LOCALAPPDATA%\\LiveTelemetryEvo\\logs\\``.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .paths import logs_dir as _primary_logs_dir

_LOG_NAME = "session.log"

_lock = threading.Lock()
_fp = None
_resolved_log_dir: Path | None = None
_last_retry: float = 0.0
_RETRY_COOLDOWN_S = 10.0


def _fallback_logs_dir() -> Path:
    """``%LOCALAPPDATA%/LiveTelemetryEvo/logs`` — writable even when the
    exe directory is UAC-protected."""
    base = os.environ.get("LOCALAPPDATA", "")
    if not base:
        base = os.path.expanduser("~")
    path = Path(base) / "LiveTelemetryEvo" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_path() -> Path | None:
    """Return the resolved log file path, or None if not yet opened."""
    if _resolved_log_dir is not None:
        return _resolved_log_dir / _LOG_NAME
    return None


def get_log_dir() -> Path | None:
    """Return the resolved log directory, or None if not yet opened."""
    return _resolved_log_dir


def _handle():
    """Return the (lazily opened) log file handle, or ``None`` if it can't
    be opened. Retries with a cooldown instead of latching failure
    permanently."""
    global _fp, _resolved_log_dir, _last_retry

    if _fp is not None:
        return _fp

    now = time.monotonic()
    if now - _last_retry < _RETRY_COOLDOWN_S:
        return None
    _last_retry = now

    for get_dir in (_primary_logs_dir, _fallback_logs_dir):
        try:
            log_dir = get_dir()
            fp = open(log_dir / _LOG_NAME, "a",
                      encoding="utf-8", buffering=1)
            _fp = fp
            _resolved_log_dir = log_dir
            return _fp
        except Exception as e:
            try:
                print(f"[logbook] cannot open {get_dir.__name__}: {e}",
                      file=sys.stderr)
            except Exception:
                pass
    return None


def log(message: str) -> None:
    """Print ``message`` and append it to ``session.log``, both prefixed
    with a bracketed local timestamp. Best-effort: never raises."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with _lock:
        try:
            print(line)
        except Exception:
            pass
        fp = _handle()
        if fp is not None:
            try:
                fp.write(line + "\n")
            except Exception:
                pass


def log_action(message: str) -> None:
    """Record one user action — :func:`log` with an ``[action]`` tag so it
    stands out in the shared session log."""
    log(f"[action] {message}")


def read_last_lines(n: int = 200) -> list[str]:
    """Return the last *n* lines from the current session log file."""
    path = get_log_path()
    if path is None or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


__all__ = ["log", "log_action", "get_log_path", "get_log_dir",
           "read_last_lines"]
