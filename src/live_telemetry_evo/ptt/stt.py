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


def _guess_language(text: str) -> str:
    """Simple heuristic: if text contains Cyrillic characters, it's Russian."""
    for ch in text:
        if "\u0400" <= ch <= "\u04ff":
            return "ru"
    return "en"
