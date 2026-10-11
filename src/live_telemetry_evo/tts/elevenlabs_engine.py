"""ElevenLabs TTS engine stub.

ElevenLabs (https://elevenlabs.io) provides high-quality cloud TTS with
support for custom/cloned voice IDs. This stub documents the interface.

To implement:
1. pip install elevenlabs
2. Set api_key in settings
3. Optionally set a custom voice_id (cloned voice)
4. Implement speak() using the elevenlabs SDK
"""
from __future__ import annotations

from typing import Any

from ..logbook import log
from .base import Priority, VoiceEngine
from .registry import register_engine


@register_engine
class ElevenLabsEngine(VoiceEngine):
    """Stub for ElevenLabs cloud TTS engine."""

    name = "elevenlabs"

    def __init__(self) -> None:
        self.api_key = ""
        self.voice_id = "21m00Tcm4TlvDq8ikWAM"  # default "Rachel"
        self.model_id = "eleven_multilingual_v2"
        self.stability = 0.5
        self.similarity_boost = 0.75

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        log(f"[tts/elevenlabs] stub: would speak '{text}' "
            f"(voice={self.voice_id}, model={self.model_id})")

    def list_voices(self) -> list[dict[str, str]]:
        return [
            {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
            {"id": "custom", "name": "Custom / Cloned Voice"},
        ]

    def settings_schema(self) -> dict[str, Any]:
        return {
            "api_key": {"type": "str", "default": "",
                        "description": "ElevenLabs API key"},
            "voice_id": {"type": "str", "default": self.voice_id,
                         "description": "Voice ID (use custom/cloned voice ID)"},
            "model_id": {"type": "choice", "default": self.model_id,
                         "options": ["eleven_multilingual_v2",
                                     "eleven_monolingual_v1",
                                     "eleven_turbo_v2"]},
            "stability": {"type": "float", "default": 0.5, "min": 0, "max": 1},
            "similarity_boost": {"type": "float", "default": 0.75,
                                 "min": 0, "max": 1},
        }
