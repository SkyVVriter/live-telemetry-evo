"""Pluggable text-to-speech subsystem.

Provides an abstract ``VoiceEngine`` interface with a registry so new
engines/voices can be added without touching the core. Ships with
``EdgeTTSEngine`` (using the ``edge-tts`` package, default
``ru-RU-DmitryNeural``), plus documented stubs for Piper and ElevenLabs.
"""
from .base import Priority, VoiceEngine
from .registry import get_engine, list_engines, register_engine
from .scheduler import SpeechMessage, SpeechScheduler

__all__ = [
    "Priority",
    "VoiceEngine",
    "SpeechMessage",
    "SpeechScheduler",
    "get_engine",
    "list_engines",
    "register_engine",
]
