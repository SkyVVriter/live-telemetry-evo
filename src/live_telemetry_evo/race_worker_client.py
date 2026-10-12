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
_CREATE_RETRIES = 4
_CREATE_BACKOFF_S = 0.5
_NO_RETRY_CODES = {400, 401, 403, 404, 422}


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _http_error_body(err: urllib.error.HTTPError) -> str:
    """Read and truncate an HTTPError response body (once)."""
    try:
        return err.read().decode("utf-8", errors="replace")[:800]
    except Exception:
        return ""


def _request(method: str, url: str, token: str,
             body: Optional[dict] = None):
    """Make an HTTP request and return parsed JSON (dict or list)."""
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


def _sanitize_token(token: str) -> str:
    """Strip whitespace and trailing punctuation from a pasted token."""
    return token.strip().rstrip(".,;:!?")


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


def map_session_type(raw: str) -> str:
    """Map an iRacing SessionType string onto the race-worker enum."""
    s = (raw or "").strip().lower()
    if "qual" in s:
        return "qualify"
    if "warm" in s or "hotlap" in s:
        return "warmup"
    if "pract" in s:
        return "practice"
    if "race" in s:
        return "race"
    return "race"


def session_payload_from_iracing(ir, *, sim: str = "iRacing") -> Optional[dict]:
    """Build a ``SessionCreate`` body from parsed iRacing session YAML.

    Returns ``None`` until the sim has connected and published at least
    a track id or track name — calling ``create_session`` before that
    produces HTTP 422 (missing required fields).
    """
    track_id = getattr(ir, "track_id_iracing", 0) or 0
    track_name = getattr(ir, "track_name", "") or ""
    if not track_id and not track_name:
        return None
    return {
        "sim": sim,
        "track_id": str(track_id or ""),
        "track_name": track_name or "unknown",
        "track_config": getattr(ir, "track_config", "") or "",
        "car": getattr(ir, "car_name", "") or "unknown",
        "series": getattr(ir, "series", "") or "",
        "session_type": map_session_type(getattr(ir, "session_type", "") or ""),
        "driver": getattr(ir, "driver", "") or "",
    }


def session_fingerprint(payload: dict) -> tuple:
    """Identity used to detect iRacing session transitions."""
    return (
        payload.get("sim"),
        payload.get("track_id"),
        payload.get("track_name"),
        payload.get("track_config"),
        payload.get("car"),
        payload.get("session_type"),
        payload.get("series"),
        payload.get("driver"),
    )


class RaceWorkerClient:
    """Thread-safe client for the race-worker REST API."""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = _sanitize_token(token)
        self._session_id: Optional[str] = None
        self._stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._on_message: Optional[Callable[[dict], None]] = None
        self._last_event_id: int = 0

    def configure(self, base_url: str, token: str) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.token = _sanitize_token(token)

    @staticmethod
    def from_settings() -> "RaceWorkerClient":
        cfg = load_race_worker_settings()
        return RaceWorkerClient(
            base_url=cfg["url"],
            token=cfg["app_token"],
        )

    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity AND authentication.

        Hits ``GET /race/sessions`` which requires a valid Bearer token.
        A 401 from this endpoint means the token is wrong; any other
        HTTP error or network failure is reported as-is.
        The ``/health`` endpoint is unauthenticated and must not be used
        to verify the token.
        """
        try:
            sessions = self.list_sessions()
            n = len(sessions)
            return True, f"OK (authenticated, {n} sessions)"
        except urllib.error.HTTPError as e:
            body = _http_error_body(e)
            if e.code == 401:
                return False, "401 Unauthorized — неверный токен"
            if e.code in (404, 405):
                return False, (
                    f"HTTP {e.code}: server needs update — "
                    "обновите race-worker (GET /race/sessions)"
                )
            extra = f" {body}" if body else ""
            return False, f"HTTP {e.code}: {e.reason}{extra}"
        except Exception as e:
            return False, str(e)

    def list_sessions(self) -> list:
        """GET /race/sessions — recent sessions (auth required)."""
        url = f"{self.base_url}/race/sessions"
        resp = _request("GET", url, self.token)
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            return list(resp.get("sessions") or [])
        return []

    def _masked_token(self) -> str:
        t = self.token
        if not t:
            return "(empty)"
        if len(t) < 8:
            return "***"
        return f"…{t[-4:]}"

    def create_session(self, **kwargs) -> Optional[str]:
        """POST /race/sessions with retry/backoff. Logs 422 response body."""
        url = f"{self.base_url}/race/sessions"
        last_err: Optional[BaseException] = None
        for attempt in range(_CREATE_RETRIES):
            try:
                resp = _request("POST", url, self.token, kwargs)
                self._session_id = resp.get("id") if isinstance(resp, dict) else None
                log(f"[race-worker] session created: {self._session_id}")
                return self._session_id
            except urllib.error.HTTPError as e:
                body = _http_error_body(e)
                log(f"[race-worker] create session failed: HTTP {e.code} "
                    f"{e.reason} body={body!r} (token={self._masked_token()})")
                last_err = e
                if e.code in _NO_RETRY_CODES:
                    return None
            except Exception as e:
                last_err = e
                log(f"[race-worker] create session failed: {e} "
                    f"(token={self._masked_token()})")
            if attempt < _CREATE_RETRIES - 1:
                delay = _CREATE_BACKOFF_S * (2 ** attempt)
                log(f"[race-worker] retrying create session in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{_CREATE_RETRIES})")
                time.sleep(delay)
        if last_err is not None:
            log(f"[race-worker] create session giving up after "
                f"{_CREATE_RETRIES} attempts: {last_err}")
        return None

    def post_lap(self, lap_data: dict) -> bool:
        if not self._session_id:
            return False
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/laps"
            payload = dict(lap_data)
            if "lap" not in payload and "lap_number" in payload:
                payload["lap"] = payload.pop("lap_number")
            _request("POST", url, self.token, payload)
            return True
        except Exception as e:
            log(f"[race-worker] post lap failed: {e}")
            return False

    def post_event(self, event_data: dict) -> bool:
        if not self._session_id:
            return False
        try:
            url = f"{self.base_url}/race/sessions/{self._session_id}/events"
            payload = dict(event_data)
            if "event_type" not in payload and "type" in payload:
                payload["event_type"] = payload.pop("type")
            _request("POST", url, self.token, payload)
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


class SessionSync:
    """Create (and recreate) a race-worker session from iRacing YAML.

    ``sync`` is a no-op until ``session_payload_from_iracing`` returns a
    body. On a fingerprint change the previous session is ended and a
    new one is created.
    """

    def __init__(self, client: RaceWorkerClient) -> None:
        self.client = client
        self._fp: Optional[tuple] = None

    def sync(self, payload: Optional[dict]) -> Optional[str]:
        if not payload:
            return self.client._session_id
        fp = session_fingerprint(payload)
        if fp == self._fp and self.client._session_id:
            return self.client._session_id
        if self.client._session_id:
            log(f"[race-worker] session changed {self._fp} -> {fp}, recreating")
            self.client.end_session()
            self.client._session_id = None
        sid = self.client.create_session(**payload)
        if sid:
            self._fp = fp
        return sid
