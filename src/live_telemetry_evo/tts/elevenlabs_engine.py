"""ElevenLabs TTS engine with full implementation.

Uses the ElevenLabs REST API (no SDK dependency) for text-to-speech with
support for per-voice profiles, custom/cloned voice IDs, phrase caching,
character-usage tracking with monthly budget warnings, and automatic
fallback to EdgeTTS on network errors or quota exhaustion.

API key is read from settings or the ``ELEVENLABS_API_KEY`` env var and
is never logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..logbook import log
from .base import Priority, VoiceEngine
from .registry import register_engine

_API_BASE = "https://api.elevenlabs.io/v1"
_TIMEOUT_S = 15
_CACHE_DIR: Optional[Path] = None


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.gettempdir()) / "lte_elevenlabs_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


@dataclass
class VoiceProfile:
    """A named voice configuration for ElevenLabs."""
    name: str = "Default"
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.5
    similarity_boost: float = 0.75

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "VoiceProfile":
        return VoiceProfile(
            name=str(d.get("name", "Default")),
            voice_id=str(d.get("voice_id", "21m00Tcm4TlvDq8ikWAM")),
            model_id=str(d.get("model_id", "eleven_multilingual_v2")),
            stability=float(d.get("stability", 0.5)),
            similarity_boost=float(d.get("similarity_boost", 0.75)),
        )


DEFAULT_PROFILES: list[VoiceProfile] = [
    VoiceProfile("Rachel (EN)", "21m00Tcm4TlvDq8ikWAM",
                 "eleven_multilingual_v2", 0.5, 0.75),
    VoiceProfile("Race Engineer (RU)", "21m00Tcm4TlvDq8ikWAM",
                 "eleven_multilingual_v2", 0.3, 0.85),
]

AVAILABLE_MODELS = [
    "eleven_multilingual_v2",
    "eleven_flash_v2_5",
    "eleven_flash_v2",
    "eleven_turbo_v2_5",
    "eleven_turbo_v2",
    "eleven_monolingual_v1",
]


@dataclass
class CharacterUsage:
    """Track character usage with monthly budget."""
    chars_used: int = 0
    month_key: str = ""
    budget: int = 10000
    warned: bool = False

    def current_month_key(self) -> str:
        t = time.gmtime()
        return f"{t.tm_year}-{t.tm_mon:02d}"

    def add(self, count: int) -> None:
        mk = self.current_month_key()
        if mk != self.month_key:
            self.month_key = mk
            self.chars_used = 0
            self.warned = False
        self.chars_used += count

    @property
    def remaining(self) -> int:
        if self.current_month_key() != self.month_key:
            return self.budget
        return max(0, self.budget - self.chars_used)

    @property
    def over_budget(self) -> bool:
        return self.remaining <= 0

    def to_dict(self) -> dict:
        return {
            "chars_used": self.chars_used,
            "month_key": self.month_key,
            "budget": self.budget,
            "warned": self.warned,
        }

    @staticmethod
    def from_dict(d: dict) -> "CharacterUsage":
        return CharacterUsage(
            chars_used=int(d.get("chars_used", 0)),
            month_key=str(d.get("month_key", "")),
            budget=int(d.get("budget", 10000)),
            warned=bool(d.get("warned", False)),
        )


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _api_request(method: str, path: str, api_key: str,
                 body: Optional[bytes] = None,
                 content_type: str = "application/json",
                 accept: str = "audio/mpeg") -> bytes:
    """Make an ElevenLabs API request. Returns raw response bytes."""
    url = f"{_API_BASE}{path}"
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "xi-api-key": api_key,
            "Content-Type": content_type,
            "Accept": accept,
            "User-Agent": "LiveTelemetryEvo-TTS/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S,
                                 context=_ssl_ctx()) as resp:
        return resp.read()


def _fetch_voices_from_api(api_key: str) -> list[dict[str, str]]:
    """Fetch available voices from the ElevenLabs API."""
    raw = _api_request("GET", "/voices", api_key,
                       accept="application/json")
    data = json.loads(raw.decode("utf-8"))
    voices = data.get("voices", [])
    return [
        {"id": v.get("voice_id", ""), "name": v.get("name", "Unknown")}
        for v in voices if v.get("voice_id")
    ]


def _fetch_user_info(api_key: str) -> dict:
    """Fetch subscription/usage info from the API."""
    raw = _api_request("GET", "/user/subscription", api_key,
                       accept="application/json")
    return json.loads(raw.decode("utf-8"))


@register_engine
class ElevenLabsEngine(VoiceEngine):
    """ElevenLabs cloud TTS engine with caching and fallback."""

    name = "elevenlabs"

    def __init__(self) -> None:
        self._api_key: str = ""
        self._profiles: list[VoiceProfile] = list(DEFAULT_PROFILES)
        self._active_profile_idx: int = 0
        self._usage = CharacterUsage()
        self._playback_lock = threading.Lock()
        self._fallback_active = False
        self._load_settings()

    @property
    def api_key(self) -> str:
        if self._api_key:
            return self._api_key
        return os.environ.get("ELEVENLABS_API_KEY", "")

    @property
    def active_profile(self) -> VoiceProfile:
        idx = max(0, min(self._active_profile_idx, len(self._profiles) - 1))
        return self._profiles[idx] if self._profiles else VoiceProfile()

    def _load_settings(self) -> None:
        """Load ElevenLabs-specific settings from the JSON store."""
        try:
            from ..settings import _read
            data = _read()
            el = data.get("elevenlabs", {})
            if not isinstance(el, dict):
                return
            self._api_key = str(el.get("api_key", ""))
            profiles_raw = el.get("profiles", [])
            if isinstance(profiles_raw, list) and profiles_raw:
                self._profiles = [VoiceProfile.from_dict(p) for p in profiles_raw]
            self._active_profile_idx = int(el.get("active_profile", 0))
            usage_raw = el.get("usage", {})
            if isinstance(usage_raw, dict):
                self._usage = CharacterUsage.from_dict(usage_raw)
        except Exception:
            pass

    def _save_settings(self) -> None:
        """Persist ElevenLabs settings (API key stored but never logged)."""
        try:
            from ..settings import _read, _write
            data = _read()
            data["elevenlabs"] = {
                "api_key": self._api_key,
                "profiles": [p.to_dict() for p in self._profiles],
                "active_profile": self._active_profile_idx,
                "usage": self._usage.to_dict(),
            }
            _write(data)
        except Exception:
            pass

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        if not self.api_key:
            log("[tts/elevenlabs] no API key configured — falling back to EdgeTTS")
            self._speak_fallback(text, priority)
            return

        if self._usage.over_budget and priority < Priority.URGENT:
            log("[tts/elevenlabs] character budget exhausted — using EdgeTTS fallback")
            self._speak_fallback(text, priority)
            return

        profile = self.active_profile
        cache_path = self._cache_path(text, profile)

        if cache_path.exists():
            self._play(cache_path)
            return

        try:
            audio = self._synthesize_api(text, profile)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                log("[tts/elevenlabs] quota exceeded — falling back to EdgeTTS")
                self._fallback_active = True
            else:
                log(f"[tts/elevenlabs] network error — falling back to EdgeTTS")
            self._speak_fallback(text, priority)
            return
        except Exception as e:
            log(f"[tts/elevenlabs] synthesis error — falling back to EdgeTTS")
            self._speak_fallback(text, priority)
            return

        self._usage.add(len(text))
        self._check_budget_warning()
        self._save_settings()

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(audio)
        except OSError:
            pass

        tmp = _cache_dir() / f"_play_{os.getpid()}.mp3"
        try:
            tmp.write_bytes(audio)
            self._play(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _synthesize_api(self, text: str, profile: VoiceProfile) -> bytes:
        """Call the ElevenLabs TTS API and return raw MP3 bytes."""
        body = json.dumps({
            "text": text,
            "model_id": profile.model_id,
            "voice_settings": {
                "stability": profile.stability,
                "similarity_boost": profile.similarity_boost,
            },
        }).encode("utf-8")
        return _api_request(
            "POST",
            f"/text-to-speech/{profile.voice_id}",
            self.api_key,
            body=body,
        )

    def _cache_path(self, text: str, profile: VoiceProfile) -> Path:
        key = hashlib.md5(
            f"{profile.voice_id}:{profile.model_id}:"
            f"{profile.stability}:{profile.similarity_boost}:{text}".encode()
        ).hexdigest()
        return _cache_dir() / f"el_{key}.mp3"

    def _check_budget_warning(self) -> None:
        usage = self._usage
        if usage.warned:
            return
        threshold = int(usage.budget * 0.8)
        if usage.chars_used >= threshold:
            usage.warned = True
            pct = int(usage.chars_used / usage.budget * 100)
            log(f"[tts/elevenlabs] character budget warning: "
                f"{usage.chars_used}/{usage.budget} ({pct}%) used this month")

    def _speak_fallback(self, text: str, priority: Priority) -> None:
        """Fall back to EdgeTTS for this utterance."""
        from .registry import get_engine
        edge = get_engine("edge-tts")
        if edge is not None:
            edge.speak(text, priority)
        else:
            log("[tts/elevenlabs] EdgeTTS fallback unavailable")

    def _play(self, path: Path) -> None:
        with self._playback_lock:
            from .edge_engine import EdgeTTSEngine
            EdgeTTSEngine._play_cross_platform(path)

    def list_voices(self) -> list[dict[str, str]]:
        if self.api_key:
            try:
                return _fetch_voices_from_api(self.api_key)
            except Exception:
                pass
        result: list[dict[str, str]] = []
        for p in self._profiles:
            result.append({"id": p.voice_id, "name": p.name})
        if not result:
            result = [
                {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
                {"id": "custom", "name": "Custom / Cloned Voice"},
            ]
        return result

    def settings_schema(self) -> dict[str, Any]:
        return {
            "api_key": {"type": "str", "default": "",
                        "description": "ElevenLabs API key (env: ELEVENLABS_API_KEY)"},
            "active_profile": {"type": "int", "default": 0,
                               "description": "Index of the active voice profile"},
            "budget": {"type": "int", "default": 10000,
                       "description": "Monthly character budget"},
        }

    def configure(self, **kwargs) -> None:
        if "api_key" in kwargs:
            self._api_key = kwargs["api_key"]
        if "active_profile" in kwargs:
            self._active_profile_idx = int(kwargs["active_profile"])
        if "budget" in kwargs:
            self._usage.budget = int(kwargs["budget"])
        if "profiles" in kwargs:
            raw = kwargs["profiles"]
            if isinstance(raw, list):
                self._profiles = [
                    VoiceProfile.from_dict(p) if isinstance(p, dict) else p
                    for p in raw
                ]
        self._save_settings()

    def get_profiles(self) -> list[VoiceProfile]:
        return list(self._profiles)

    def set_profiles(self, profiles: list[VoiceProfile]) -> None:
        self._profiles = list(profiles)
        self._save_settings()

    def get_usage(self) -> CharacterUsage:
        return self._usage

    def stop(self) -> None:
        self._fallback_active = False
