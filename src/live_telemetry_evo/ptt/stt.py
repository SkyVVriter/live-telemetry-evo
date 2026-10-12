"""Pluggable speech-to-text engine interface and registry.

Ships with two implementations:
- ``WhisperAPIEngine``: OpenAI Whisper API (requires openai package + key)
- ``FasterWhisperEngine``: Local faster-whisper (optional dependency)

Language is auto-detected (ru/en) by default.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Type

from ..logbook import log


@dataclass
class TranscriptionResult:
    text: str
    language: str = ""
    duration_s: float = 0.0
    confidence: float = 0.0


class STTEngine(ABC):
    """Base class for speech-to-text engines."""

    name: str = "base"

    @abstractmethod
    def transcribe(self, wav_bytes: bytes,
                   language: str | None = None) -> TranscriptionResult:
        """Transcribe WAV audio. ``language`` is a hint (``"ru"``, ``"en"``,
        or ``None`` for auto-detect)."""

    def configure(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


_registry: dict[str, Type[STTEngine]] = {}
_instances: dict[str, STTEngine] = {}


def register_stt_engine(cls: Type[STTEngine]) -> Type[STTEngine]:
    _registry[cls.name] = cls
    return cls


def get_stt_engine(name: str) -> Optional[STTEngine]:
    if name in _instances:
        return _instances[name]
    cls = _registry.get(name)
    if cls is None:
        return None
    inst = cls()
    _instances[name] = inst
    return inst


def list_stt_engines() -> list[str]:
    return list(_registry.keys())


@register_stt_engine
class WhisperAPIEngine(STTEngine):
    """OpenAI Whisper API speech-to-text."""

    name = "whisper-api"

    def __init__(self) -> None:
        self.api_key: str = ""
        self.model: str = "whisper-1"

    def transcribe(self, wav_bytes: bytes,
                   language: str | None = None) -> TranscriptionResult:
        import io
        try:
            import openai  # type: ignore[import-untyped]
        except ImportError:
            log("[stt/whisper-api] openai package not installed")
            return TranscriptionResult(text="", language=language or "")

        api_key = self.api_key
        if not api_key:
            import os
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            log("[stt/whisper-api] no API key configured")
            return TranscriptionResult(text="", language=language or "")

        client = openai.OpenAI(api_key=api_key)
        audio_file = io.BytesIO(wav_bytes)
        audio_file.name = "recording.wav"

        kwargs: dict = {"model": self.model, "file": audio_file}
        if language:
            kwargs["language"] = language

        try:
            response = client.audio.transcriptions.create(**kwargs)
            text = response.text.strip() if hasattr(response, "text") else str(response).strip()
            detected_lang = language or _guess_language(text)
            return TranscriptionResult(
                text=text,
                language=detected_lang,
            )
        except Exception as e:
            log(f"[stt/whisper-api] transcription failed: {e}")
            return TranscriptionResult(text="", language=language or "")


@register_stt_engine
class FasterWhisperEngine(STTEngine):
    """Local faster-whisper speech-to-text."""

    name = "faster-whisper"

    def __init__(self) -> None:
        self.model_size: str = "base"
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-untyped]
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8")
            except ImportError:
                log("[stt/faster-whisper] faster-whisper package not installed")
                return None
            except Exception as e:
                log(f"[stt/faster-whisper] model load failed: {e}")
                return None
        return self._model

    def transcribe(self, wav_bytes: bytes,
                   language: str | None = None) -> TranscriptionResult:
        import io
        import tempfile
        from pathlib import Path

        model = self._get_model()
        if model is None:
            return TranscriptionResult(text="", language=language or "")

        tmp = Path(tempfile.gettempdir()) / "lte_stt_input.wav"
        tmp.write_bytes(wav_bytes)

        try:
            segments, info = model.transcribe(
                str(tmp), language=language, beam_size=5)
            text = " ".join(seg.text.strip() for seg in segments)
            return TranscriptionResult(
                text=text.strip(),
                language=info.language or language or "",
                duration_s=info.duration,
            )
        except Exception as e:
            log(f"[stt/faster-whisper] transcription failed: {e}")
            return TranscriptionResult(text="", language=language or "")


@register_stt_engine
class ElevenLabsScribeEngine(STTEngine):
    """ElevenLabs Scribe speech-to-text (``scribe_v1`` model).

    Uses the ElevenLabs REST API directly (no SDK).  API key is read
    from PTT settings first; if empty, falls back to the shared
    ElevenLabs TTS key in the ``elevenlabs`` settings block, then to
    the ``ELEVENLABS_API_KEY`` env var.  The key is never logged.
    """

    name = "elevenlabs-scribe"

    def __init__(self) -> None:
        self.api_key: str = ""
        self.model_id: str = "scribe_v1"
        self.language_code: str = ""

    def _resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        try:
            from ..settings import _read
            data = _read()
            el = data.get("elevenlabs", {})
            if isinstance(el, dict):
                key = el.get("api_key", "")
                if key:
                    return str(key)
        except Exception:
            pass
        import os
        return os.environ.get("ELEVENLABS_API_KEY", "")

    def transcribe(self, wav_bytes: bytes,
                   language: str | None = None) -> TranscriptionResult:
        import io
        import json
        import ssl
        import urllib.error
        import urllib.request

        api_key = self._resolve_api_key()
        if not api_key:
            log("[stt/elevenlabs-scribe] no API key configured")
            return TranscriptionResult(text="", language=language or "")

        boundary = "----LTEScribeBoundary"
        body = io.BytesIO()

        def _field(name: str, value: str) -> None:
            body.write(f"--{boundary}\r\n".encode())
            body.write(
                f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                .encode())
            body.write(f"{value}\r\n".encode())

        _field("model_id", self.model_id)
        lang = language or self.language_code
        if lang:
            _field("language_code", lang)

        body.write(f"--{boundary}\r\n".encode())
        body.write(
            b"Content-Disposition: form-data; name=\"file\"; "
            b"filename=\"recording.wav\"\r\n"
            b"Content-Type: audio/wav\r\n\r\n")
        body.write(wav_bytes)
        body.write(b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())

        payload = body.getvalue()
        url = "https://api.elevenlabs.io/v1/speech-to-text"
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "xi-api-key": api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
                "User-Agent": "LiveTelemetryEvo-STT/1.0",
            },
        )
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=10,
                                        context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = str(data.get("text", "")).strip()
            detected = language or lang or _guess_language(text)
            return TranscriptionResult(text=text, language=detected)
        except urllib.error.HTTPError as e:
            status = e.code
            log(f"[stt/elevenlabs-scribe] HTTP {status}")
            return TranscriptionResult(text="", language=language or "")
        except Exception as e:
            err_type = type(e).__name__
            log(f"[stt/elevenlabs-scribe] request failed: {err_type}")
            return TranscriptionResult(text="", language=language or "")


def _guess_language(text: str) -> str:
    """Simple heuristic: if text contains Cyrillic characters, it's Russian."""
    for ch in text:
        if "\u0400" <= ch <= "\u04ff":
            return "ru"
    return "en"
