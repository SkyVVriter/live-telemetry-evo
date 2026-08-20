"""Async Cloud Dispatcher for uploading completed telemetry sessions to backend."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .logbook import log
from .settings import load_cloud_settings


class CloudDispatcher:
    """Handles packaging and async HTTP upload of telemetry files."""

    @staticmethod
    def upload_async(
        csv_path: Path,
        on_complete: Optional[Callable[[bool, str], None]] = None
    ) -> None:
        """Compress CSV into zip and upload to server on background thread."""
        thread = threading.Thread(
            target=CloudDispatcher._upload_worker,
            args=(csv_path, on_complete),
            name="cloud-dispatcher",
            daemon=True,
        )
        thread.start()

    @staticmethod
    def _upload_worker(
        csv_path: Path,
        on_complete: Optional[Callable[[bool, str], None]] = None
    ) -> None:
        cfg = load_cloud_settings()
        if not cfg.get("auto_upload"):
            log("Cloud upload skipped (auto_upload is disabled)")
            if on_complete:
                on_complete(True, "Skipped (auto_upload disabled)")
            return

        server_url = cfg.get("server_url", "").rstrip("/")
        api_token = cfg.get("api_token", "").strip()
        pilot_id = cfg.get("pilot_id", "Pilot").strip()

        if not server_url or not api_token:
            msg = "Missing Server URL or API Token in settings"
            log(f"Cloud upload error: {msg}")
            if on_complete:
                on_complete(False, msg)
            return

        if not csv_path.exists():
            msg = f"CSV file not found: {csv_path}"
            log(f"Cloud upload error: {msg}")
            if on_complete:
                on_complete(False, msg)
            return

        # Compress into zip
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
            return

        # Upload via multipart HTTP POST
        upload_endpoint = f"{server_url}/api/v1/telemetry/upload"
        try:
            boundary = "----WebKitFormBoundaryAI725eTelemetry"
            body = bytearray()

            # Pilot ID form field
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(b'Content-Disposition: form-data; name="pilot_id"\r\n\r\n')
            body.extend(f"{pilot_id}\r\n".encode("utf-8"))

            # Zip file part
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="file"; filename="{zip_path.name}"\r\n'.encode("utf-8"))
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
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=30.0) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                report_url = resp_data.get("report_url", "")
                best_lap = resp_data.get("best_lap_time", 0.0)
                msg = f"Uploaded successfully! Best: {best_lap:.3f}s. Report: {report_url}"
                log(f"Cloud Telemetry Dispatch: {msg}")
                if on_complete:
                    on_complete(True, msg)
        except Exception as e:
            msg = f"Upload failed: {e}"
            log(f"Cloud Telemetry Dispatch Error: {msg}")
            if on_complete:
                on_complete(False, msg)

    @staticmethod
    def test_connection(server_url: str, api_token: str) -> tuple[bool, str]:
        """Test connection to server health endpoint."""
        url = f"{server_url.rstrip('/')}/api/v1/health"
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_token}"},
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return True, f"Online ({data.get('service', 'OK')})"
                return False, f"HTTP {resp.status}"
        except Exception as e:
            return False, str(e)
