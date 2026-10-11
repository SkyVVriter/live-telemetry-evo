"""TTS engine registry: register and retrieve engines by name."""
from __future__ import annotations

from typing import Optional, Type

from .base import VoiceEngine

_registry: dict[str, Type[VoiceEngine]] = {}
_instances: dict[str, VoiceEngine] = {}


def register_engine(cls: Type[VoiceEngine]) -> Type[VoiceEngine]:
    """Register a VoiceEngine subclass. Can be used as a decorator."""
    _registry[cls.name] = cls
    return cls


def get_engine(name: str) -> Optional[VoiceEngine]:
    """Get or create a singleton instance of the named engine."""
    if name in _instances:
        return _instances[name]
    cls = _registry.get(name)
    if cls is None:
        return None
    inst = cls()
    _instances[name] = inst
    return inst


def list_engines() -> list[str]:
    """Return names of all registered engines."""
    return list(_registry.keys())
