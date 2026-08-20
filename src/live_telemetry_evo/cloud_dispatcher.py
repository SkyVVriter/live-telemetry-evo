"""Async Cloud Dispatcher for uploading completed telemetry sessions to backend."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .logbook import log
from .paths import logs_dir
from .settings import load_cloud_settings


MAX_ATTEMPTS = 5
BACKOFF_SEC = (2.0, 4.0, 8.0, 16.0, 32.0)
MIN_TIMEOUT_SEC = 180.0
MAX_TIMEOUT_SEC = 900.0
SEC_PER_MB = 12.0
MIN_CSV_BYTES = 1024


def _uploaded_marker(csv_path: Path) -> Path:
    return Path(str(csv_path) + ".uploaded")


def _timeout_for(zip_path: Path) -> float:
    size_mb = max(zip_path.stat().st_size / (1024 * 1024), 1.0)
    return min(MAX_TIMEOUT_SEC, max(MIN_TIMEOUT_SEC, size_mb * SEC_PER_MB))


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in (408, 429)
    return True


def _write_uploaded_marker(csv_path: Path, message: str) -> None:
    payload = {
        "ok": True,
        "message": message,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _uploaded_marker(csv_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


class CloudDispatcher:
    """Handles packaging and async HTTP upload of telemetry files."""

    @staticmethod
    def pending_session_files(extra_dirs: Optional[Iterable[Path]] = None) -> list[Path]:
        """CSV logs that have not been marked as successfully uploaded."""
        dirs = [logs_dir()]
        if extra_dirs:
            dirs.extend(Path(d) for d in extra_dirs)
        seen: set[Path] = set()
        pending: list[Path] = []
        for folder in dirs:
            try:
                if not folder.exists():
                    continue
            except OSError:
                continue
            for path in sorted(folder.glob("*.csv")):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    if path.stat().st_size < MIN_CSV_BYTES:
                        continue
                except OSError:
                    continue
                if _uploaded_marker(path).exists():
                    continue
                pending.append(path)
        return pending

    @staticmethod
    def upload_async(
        csv_path: Path,
        on_complete: Optional[Callable[[bool, str], None]] = None,
        force: bool = False,
    ) -> None:
        """Compress CSV into zip and upload to server on a background thread."""
        thread = threading.Thread(
            target=CloudDispatcher._upload_worker,
            args=(csv_path, on_complete, force),
            name="cloud-dispatcher",
            daemon=True,
        )
        thread.start()

    @staticmethod
    def upload_pending_async(
        on_progress: Optional[Callable[[str], None]] = None,
        on_complete: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        """Upload every local CSV that is not yet marked uploaded.

        Manual catch-up for sessions recorded offline or after a failed
        auto-upload. Ignores the auto_upload setting.
        """
        thread = threading.Thread(
            target=CloudDispatcher._upload_pending_worker,
            args=(on_progress, on_complete),
            name="cloud-dispatcher-pending",
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _upload_pending_worker(
        on_progress: Optional[Callable[[str], None]],
        on_complete: Optional[Callable[[int, int, str], None]],
    ) -> None:
        pending = CloudDispatcher.pending_session_files()
        if not pending:
            msg = "Нет локальных сессий, ожидающих загрузки"
            log(f"Cloud Telemetry Dispatch: {msg}")
            if on_complete:
                on_complete(0, 0, msg)
            return

        ok_n = 0
        fail_n = 0
        last_err = ""
        total = len(pending)
        for i, csv_path in enumerate(pending, start=1):
            if on_progress:
                on_progress(f"Загрузка {i}/{total}: {csv_path.name}")
            log(f"Cloud Telemetry Dispatch: pending upload {i}/{total} {csv_path.name}")
            success, message = CloudDispatcher._upload_worker(csv_path, None, True)
            if success:
                ok_n += 1
            else:
                fail_n += 1
                last_err = message
        summary = f"Загружено {ok_n} из {total}"
        if fail_n:
            summary += f", ошибок: {fail_n}"
            if last_err:
                summary += f" (последняя: {last_err})"
        log(f"Cloud Telemetry Dispatch: {summary}")
        if on_complete:
            on_complete(ok_n, fail_n, summary)

    @staticmethod
    def _upload_worker(
        csv_path: Path,
        on_complete: Optional[Callable[[bool, str], None]] = None,
        force: bool = False,
    ) -> tuple[bool, str]:
        cfg = load_cloud_settings()
        if not force and not cfg.get("auto_upload"):
            log("Cloud upload skipped (auto_upload is disabled)")
            if on_complete:
                on_complete(True, "Skipped (auto_upload disabled)")
            return True, "Skipped (auto_upload disabled)"

        server_url = cfg.get("server_url", "").rstrip("/")
        api_token = cfg.get("api_token", "").strip()
        pilot_id = cfg.get("pilot_id", "Pilot").strip()

        if not server_url or not api_token:
            msg = "Missing Server URL or API Token in settings"
            log(f"Cloud upload error: {msg}")
            if on_complete:
                on_complete(False, msg)
            return False, msg

        csv_path = Path(csv_path)
        if not csv_path.exists():
            msg = f"CSV file not found: {csv_path}"
            log(f"Cloud upload error: {msg}")
            if on_complete:
                on_complete(False, msg)
            return False, msg

        zip_path = csv_path.with_suffix(".zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(csv_path, arcname=csv_path.name)
            log(f"Compressed telemetry to {zip_path.name} ({zip_path.stat().st_size / 1024:.1f} KB)")
        except Exception as e:
            msg = f"Zip compression failed: {e}"
            log(f"Cloud upload error: {msg}")
            if on_complete:
                on_complete(False, msg)
            return False, msg

        upload_endpoint = f"{server_url}/api/v1/telemetry/upload"
        timeout = _timeout_for(zip_path)
        last_error = "Upload failed"
        success = False
        success_msg = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                boundary = "----WebKitFormBoundaryAI725eTelemetry"
                body = bytearray()

                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(b'Content-Disposition: form-data; name="pilot_id"\r\n\r\n')
                body.extend(f"{pilot_id}\r\n".encode("utf-8"))

                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(
                    f'Content-Disposition: form-data; name="file"; filename="{zip_path.name}"\r\n'.encode("utf-8")
                )
                body.extend(b"Content-Type: application/zip\r\n\r\n")
                with open(zip_path, "rb") as f:
                    body.extend(f.read())
                body.extend(b"\r\n")
                body.extend(f"--{boundary}--\r\n".encode("utf-8"))

                req = urllib.request.Request(
                    upload_endpoint,
                    data=bytes(body),
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Authorization": f"Bearer {api_token}",
                        "User-Agent": "LiveTelemetryEvo-Pro/2.0",
                    },
                    method="POST",
                )

                log(
                    f"Cloud Telemetry Dispatch: uploading {zip_path.name} "
                    f"attempt {attempt}/{MAX_ATTEMPTS} timeout={timeout:.0f}s"
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    report_url = resp_data.get("report_url", "")
                    best_lap = resp_data.get("best_lap_time", 0.0)
                    track_name = resp_data.get("track_name", "")
                    extra = f" {track_name}." if track_name else "."
                    success_msg = (
                        f"Uploaded successfully! Best: {best_lap:.3f}s.{extra} Report: {report_url}"
                    )
                    log(f"Cloud Telemetry Dispatch: {success_msg}")
                    _write_uploaded_marker(csv_path, success_msg)
                    success = True
                    break
            except Exception as e:
                last_error = f"Upload failed: {e}"
                retry = attempt < MAX_ATTEMPTS and _is_retryable(e)
                log(
                    f"Cloud Telemetry Dispatch Error: {last_error}"
                    + (f" (retry {attempt}/{MAX_ATTEMPTS})" if retry else "")
                )
                if not retry:
                    break
                time.sleep(BACKOFF_SEC[min(attempt - 1, len(BACKOFF_SEC) - 1)])

        if success:
            if on_complete:
                on_complete(True, success_msg)
            return True, success_msg

        if on_complete:
            on_complete(False, last_error)
        return False, last_error

    @staticmethod
    def test_connection(server_url: str, api_token: str) -> tuple[bool, str]:
        """Test connection to server health endpoint."""
        url = f"{server_url.rstrip('/')}/api/v1/health"
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return True, f"Online ({data.get('service', 'OK')})"
                return False, f"HTTP {resp.status}"
        except Exception as e:
            return False, str(e)
