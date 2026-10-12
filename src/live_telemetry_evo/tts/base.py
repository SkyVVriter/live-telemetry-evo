"""Abstract VoiceEngine interface for the TTS subsystem."""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import IntEnum
from typing import Any, Optional


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


class VoiceEngine(ABC):
    """Base class for all TTS engines.

    Subclasses must implement ``speak``, ``list_voices``, and
    ``settings_schema``. The engine is instantiated once and reused;
    ``configure`` is called whenever settings change.
    """

    name: str = "base"

    @abstractmethod
    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        """Speak ``text`` (blocking). Priority may influence voice style
        or audio post-processing in some engines."""

    @abstractmethod
    def list_voices(self) -> list[dict[str, str]]:
        """Return available voices as ``[{"id": ..., "name": ...}, ...]``."""

    @abstractmethod
    def settings_schema(self) -> dict[str, Any]:
        """Return a dict describing configurable parameters, e.g.::

            {"voice": {"type": "choice", "options": [...]},
             "rate": {"type": "str", "default": "+0%"},
             "volume": {"type": "str", "default": "+0%"}}
        """

    def configure(self, **kwargs) -> None:
        """Apply settings from the UI. Default: store as attributes."""
        for k, v in kwargs.items():
            setattr(self, k, v)

    def stop(self) -> None:
        """Stop any current playback. Default no-op."""


def _log_import_error(mod_name: str, error: Exception) -> None:
    """Log a TTS engine import failure without crashing."""
    try:
        from ..logbook import log
        log(f"[tts] engine module '{mod_name}' import failed: {error}")
    except Exception:
        pass
