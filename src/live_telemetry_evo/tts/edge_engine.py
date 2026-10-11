"""Edge TTS engine using the ``edge-tts`` package.

Default voice: ``ru-RU-DmitryNeural``. Configurable voice, rate, volume,
pitch. Includes an audio cache for frequent phrases and playback on a
separate thread.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from ..logbook import log
from .base import Priority, VoiceEngine
from .registry import register_engine

_CACHE_DIR: Optional[Path] = None


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.gettempdir()) / "lte_tts_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


@register_engine
class EdgeTTSEngine(VoiceEngine):
    """TTS via Microsoft Edge's online TTS (edge-tts package)."""

    name = "edge-tts"

    def __init__(self) -> None:
        self.voice = "ru-RU-DmitryNeural"
        self.rate = "+0%"
        self.volume = "+0%"
        self.pitch = "+0Hz"
        self._playback_lock = threading.Lock()

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        cache_key = hashlib.md5(
            f"{self.voice}:{self.rate}:{self.volume}:{text}".encode()
        ).hexdigest()
        cache_path = _get_cache_dir() / f"{cache_key}.mp3"

        if not cache_path.exists():
            try:
                self._synthesize(text, cache_path)
            except Exception as e:
                log(f"[tts/edge] synthesis failed: {e}")
                return

        self._play(cache_path)

    def _synthesize(self, text: str, out_path: Path) -> None:
        try:
            import asyncio
            import edge_tts  # type: ignore[import-untyped]
        except ImportError:
            log("[tts/edge] edge-tts package not installed")
            raise

        async def _do():
            comm = edge_tts.Communicate(
                text, self.voice,
                rate=self.rate, volume=self.volume, pitch=self.pitch,
            )
            await comm.save(str(out_path))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_do())
        finally:
            loop.close()

    def _play(self, path: Path) -> None:
        with self._playback_lock:
            try:
                if os.name == "nt":
                    import winsound  # type: ignore[import-error]
                    # winsound can't play mp3; use pygame or playsound fallback
                    self._play_cross_platform(path)
                else:
                    self._play_cross_platform(path)
            except Exception as e:
                log(f"[tts/edge] playback failed: {e}")

    @staticmethod
    def _play_cross_platform(path: Path) -> None:
        """Play audio using the first available method."""
        try:
            import subprocess
            import sys
            if sys.platform == "win32":
                # PowerShell can play mp3
                subprocess.run(
                    ["powershell", "-c",
                     f'(New-Object Media.SoundPlayer "{path}").PlaySync()'],
                    timeout=30, check=False, capture_output=True,
                )
            elif sys.platform == "darwin":
                subprocess.run(["afplay", str(path)], timeout=30, check=False)
            else:
                # Linux: try mpv, then ffplay, then aplay
                for player in ("mpv --no-video", "ffplay -nodisp -autoexit",
                               "aplay"):
                    parts = player.split() + [str(path)]
                    try:
                        subprocess.run(parts, timeout=30, check=True,
                                       capture_output=True)
                        return
                    except (FileNotFoundError, subprocess.CalledProcessError):
                        continue
        except Exception:
            pass

    def list_voices(self) -> list[dict[str, str]]:
        try:
            import asyncio
            import edge_tts
            voices = asyncio.get_event_loop().run_until_complete(
                edge_tts.list_voices())
            return [{"id": v["ShortName"], "name": v["FriendlyName"]}
                    for v in voices]
        except Exception:
            return [
                {"id": "ru-RU-DmitryNeural", "name": "Dmitry (RU)"},
                {"id": "ru-RU-SvetlanaNeural", "name": "Svetlana (RU)"},
                {"id": "en-US-GuyNeural", "name": "Guy (EN-US)"},
                {"id": "en-GB-RyanNeural", "name": "Ryan (EN-GB)"},
            ]

    def settings_schema(self) -> dict[str, Any]:
        return {
            "voice": {"type": "choice", "default": self.voice,
                      "options": [v["id"] for v in self.list_voices()]},
            "rate": {"type": "str", "default": "+0%",
                     "description": "Speech rate, e.g. +20%, -10%"},
            "volume": {"type": "str", "default": "+0%",
                       "description": "Volume adjustment, e.g. +50%"},
            "pitch": {"type": "str", "default": "+0Hz",
                      "description": "Pitch adjustment, e.g. +5Hz"},
        }

    def configure(self, **kwargs) -> None:
        if "voice" in kwargs:
            self.voice = kwargs["voice"]
        if "rate" in kwargs:
            self.rate = kwargs["rate"]
        if "volume" in kwargs:
            self.volume = kwargs["volume"]
        if "pitch" in kwargs:
            self.pitch = kwargs["pitch"]
