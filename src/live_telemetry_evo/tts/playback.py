"""In-process TTS playback. Never opens a PowerShell / console window.

Backends, in order:
1. ``winsound`` for WAV on Windows (stdlib, no extra DLL).
2. ``pygame.mixer`` for MP3/WAV (bundled in the frozen exe).
3. ``sounddevice`` for WAV when numpy is available.

Any leftover subprocess (Linux helpers only) uses ``CREATE_NO_WINDOW``
on Windows so a console cannot flash.
"""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

from ..logbook import log

PCM_RATE_HZ = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2  # 16-bit


def pcm16_to_wav(pcm: bytes, out_path: Path,
                 rate: int = PCM_RATE_HZ,
                 channels: int = PCM_CHANNELS) -> Path:
    """Wrap raw little-endian PCM16 in a WAV container."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(PCM_SAMPLE_WIDTH)
        w.setframerate(rate)
        w.writeframes(pcm)
    log(f"[tts/play] wrote wav {out_path.name} "
        f"({len(pcm)} pcm bytes, {rate} Hz)")
    return out_path


def play_audio_file(path: Path) -> None:
    """Play ``path`` in-process. Raises ``RuntimeError`` if every backend fails."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"audio file missing: {path}")
    size = path.stat().st_size
    log(f"[tts/play] start {path.name} ({size} bytes, suffix={path.suffix})")
    if size == 0:
        raise RuntimeError(f"audio file is empty: {path}")

    errors: list[str] = []

    if path.suffix.lower() == ".wav" and sys.platform == "win32":
        try:
            _play_winsound(path)
            log("[tts/play] winsound OK")
            return
        except Exception as e:
            log(f"[tts/play] winsound failed: {e}")
            errors.append(f"winsound: {e}")

    try:
        _play_pygame(path)
        log("[tts/play] pygame.mixer OK")
        return
    except Exception as e:
        log(f"[tts/play] pygame.mixer failed: {e}")
        errors.append(f"pygame: {e}")

    if path.suffix.lower() == ".wav":
        try:
            _play_sounddevice_wav(path)
            log("[tts/play] sounddevice OK")
            return
        except Exception as e:
            log(f"[tts/play] sounddevice failed: {e}")
            errors.append(f"sounddevice: {e}")

    raise RuntimeError("all playback backends failed: " + "; ".join(errors))


def _play_winsound(path: Path) -> None:
    import winsound  # type: ignore[import-not-found]
    winsound.PlaySound(str(path), winsound.SND_FILENAME)


def _play_pygame(path: Path) -> None:
    import pygame
    if pygame.mixer.get_init() is None:
        pygame.mixer.init()
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()
    clock = pygame.time.Clock()
    while pygame.mixer.music.get_busy():
        clock.tick(40)


def _play_sounddevice_wav(path: Path) -> None:
    import sounddevice as sd  # type: ignore
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
        width = w.getsampwidth()
        channels = w.getnchannels()
    if width != 2:
        raise RuntimeError(f"unsupported WAV sample width {width}")
    try:
        import numpy as np
        data = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            data = data.reshape(-1, channels)
        sd.play(data, samplerate=rate)
        sd.wait()
        return
    except ImportError:
        pass
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    sd.play(samples, samplerate=rate)
    sd.wait()


def hidden_run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    """subprocess.run that never flashes a console window on Windows."""
    kwargs: dict = {
        "timeout": timeout,
        "check": False,
        "capture_output": True,
    }
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs["startupinfo"] = startup
    return subprocess.run(cmd, **kwargs)
