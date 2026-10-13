"""Pluggable text-to-speech subsystem.

Provides an abstract ``VoiceEngine`` interface with a registry so new
engines/voices can be added without touching the core. Ships with
``EdgeTTSEngine`` (using the ``edge-tts`` package, default
``ru-RU-DmitryNeural``), ``ElevenLabsEngine``, and ``PiperEngine``.

All engine modules are imported eagerly so their ``@register_engine``
decorators fire and ``list_engines()`` returns the full set.
"""
from .base import Priority, VoiceEngine
from .preprocess import SSML_BREAK, preprocess_tts_text
from .registry import get_engine, list_engines, register_engine
from .scheduler import SpeechMessage, SpeechScheduler

# Import every engine module so @register_engine runs at startup.
# Guarded so a missing optional dependency (e.g. edge-tts package)
# doesn't prevent the rest of the app from loading.
# piper_engine is a stub and must not log an error if the frozen exe
# omitted it — degrade silently.
_OPTIONAL_ENGINES = frozenset({"piper_engine"})


def _register_all_engines() -> None:
    import importlib
    for mod_name in ("edge_engine", "elevenlabs_engine", "piper_engine"):
        try:
            importlib.import_module(f".{mod_name}", __package__)
        except Exception as _e:
            if mod_name in _OPTIONAL_ENGINES:
                continue
            try:
                from .base import _log_import_error
                _log_import_error(mod_name, _e)
            except Exception:
                pass

_register_all_engines()

__all__ = [
    "Priority",
    "VoiceEngine",
    "SpeechMessage",
    "SpeechScheduler",
    "SSML_BREAK",
    "preprocess_tts_text",
    "get_engine",
    "list_engines",
    "register_engine",
]
