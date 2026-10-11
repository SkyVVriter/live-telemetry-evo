"""Piper TTS engine stub.

Piper (https://github.com/rhasspy/piper) is a fast local neural TTS.
This stub documents the interface; a full implementation would load a
``.onnx`` voice model and run inference locally (no network required).

To implement:
1. pip install piper-tts
2. Download a voice model (.onnx + .json)
3. Configure model_path in settings
4. Implement speak() using piper.PiperVoice
"""
from __future__ import annotations

from typing import Any

from ..logbook import log
from .base import Priority, VoiceEngine
from .registry import register_engine


@register_engine
class PiperEngine(VoiceEngine):
    """Stub for Piper local TTS engine."""

    name = "piper"

    def __init__(self) -> None:
        self.model_path = ""
        self.speaker_id = 0

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        log(f"[tts/piper] stub: would speak '{text}' "
            f"(model={self.model_path}, speaker={self.speaker_id})")

    def list_voices(self) -> list[dict[str, str]]:
        return [
            {"id": "ru_RU-ruslan-medium", "name": "Ruslan (RU, medium)"},
            {"id": "en_US-lessac-medium", "name": "Lessac (EN-US, medium)"},
        ]

    def settings_schema(self) -> dict[str, Any]:
        return {
            "model_path": {"type": "str", "default": "",
                           "description": "Path to .onnx voice model"},
            "speaker_id": {"type": "int", "default": 0,
                           "description": "Speaker ID for multi-speaker models"},
        }
