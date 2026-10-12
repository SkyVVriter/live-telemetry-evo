"""Background client for the race-worker relay service.

Runs on a dedicated thread, POSTing lap data and events to the remote
race-worker API and polling for TTS messages from the AI assistant.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .logbook import log
from .settings import load_race_worker_settings


_TIMEOUT_S = 8
_POLL_INTERVAL_S = 3.0
_USER_AGENT = "LiveTelemetryEvo-RaceWorkerClient/1.0"


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _request(method: str, url: str, token: str,
             body: Optional[dict] = None) -> dict:
    """Make an HTTP request and return parsed JSON."""
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_ctx()) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def _normalize_base_url(url: str) -> str:
    """Accept URLs with or without trailing ``/race`` (and trailing slash).

    The API endpoints all live under ``<base>/race/...``, so the caller
    should be able to pass ``https://host``, ``https://host/``,
    ``https://host/race``, or ``https://host/race/`` and get the same
    result.  We strip any trailing ``/race`` suffix so the rest of the
    class can unconditionally prepend ``/race/...``.
    """
    url = url.strip().rstrip("/")
    if url.endswith("/race"):
        url = url[:-5].rstrip("/")
    return url


class RaceWorkerClient:
    """Thread-safe client for the race-worker REST API."""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = token
        self._session_id: Optional[str] = None
        self._stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._on_message: Optional[Callable[[dict], None]] = None
        self._last_event_id: int = 0

    def configure(self, base_url: str, token: str) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = token

    @staticmethod
    def from_settings() -> "RaceWorkerClient":
        cfg = load_race_worker_settings()
        return RaceWorkerClient(
            base_url=cfg["url"],
            token=cfg["app_token"],
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            url = f"{self.base_url}/race/health"
            resp = _request("GET", url, self.token)
            return True, f"OK ({resp.get('version', '?')})"
        except Exception as e:
            return False, str(e)

    def _masked_token(self) -> str:
        t = self.token
        if not t:
            return "(empty)"
        if len(t) < 8:
            return "***"
        return f"…{t[-4:]}"

    def create_session(self, **kwargs) -> Optional[str]:
        try:
            url = f"{self.base_url}/race/sessions"
            resp = _request("POST", url, self.token, kwargs)
            self._session_id = resp.get("id")
            log(f"[race-worker] session created: {self._session_id}")
            return self._session_id
        except Exception as e:
            log(f"[race-worker] create session failed: {e} "
                f"(token={self._masked_token()})")
            return None

    def post_lap(self, lap_data: dict) -> bool:
        if not self._session_id:
            return False
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/laps"
            _request("POST", url, self.token, lap_data)
            return True
        except Exception as e:
            log(f"[race-worker] post lap failed: {e}")
            return False

    def post_event(self, event_data: dict) -> bool:
        if not self._session_id:
            return False
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/events"
            _request("POST", url, self.token, event_data)
            return True
        except Exception as e:
            log(f"[race-worker] post event failed: {e}")
            return False

    def poll_messages(self) -> list[dict]:
        if not self._session_id:
            return []
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/messages/next"
            resp = _request("GET", url, self.token)
            return resp if isinstance(resp, list) else []
        except Exception:
            return []

    def ack_message(self, message_id: int) -> bool:
        if not self._session_id:
            return False
        try:
            url = (f"{self.base_url}/race/sessions/{self._session_id}"
                   f"/messages/{message_id}/ack")
            _request("POST", url, self.token)
            return True
        except Exception:
            return False

    def end_session(self) -> bool:
        if not self._session_id:
            return False
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/end"
            _request("POST", url, self.token)
            log(f"[race-worker] session ended: {self._session_id}")
            return True
        except Exception as e:
            log(f"[race-worker] end session failed: {e}")
            return False

    def start_polling(self, on_message: Callable[[dict], None]) -> None:
        """Start background polling for messages."""
        self._on_message = on_message
        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="race-worker-poll", daemon=True)
        self._poll_thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                messages = self.poll_messages()
                for msg in messages:
                    mid = msg.get("id")
                    if self._on_message:
                        self._on_message(msg)
                    if mid:
                        self.ack_message(mid)
            except Exception as e:
                log(f"[race-worker] poll error: {e}")
            self._stop.wait(_POLL_INTERVAL_S)
