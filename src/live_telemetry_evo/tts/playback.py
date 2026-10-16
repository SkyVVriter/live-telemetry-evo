"""In-process TTS playback. Never opens a PowerShell / console window.

Backends, in order:
1. ``sounddevice`` to a *selected* Windows render endpoint (when the
   Radio (PTT) output device is set). ``winsound`` / ``pygame.mixer``
   always play to the Windows default device and are skipped in this
   case.
2. ``winsound`` for WAV on Windows (stdlib, no extra DLL) — Windows
   default endpoint only.
3. ``pygame.mixer`` for MP3/WAV (bundled in the frozen exe) — default
   endpoint.
4. ``sounddevice`` for WAV when numpy is available — default endpoint.

Any leftover subprocess (Linux helpers only) uses ``CREATE_NO_WINDOW``
on Windows so a console cannot flash.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import wave
from pathlib import Path

from ..logbook import log

PCM_RATE_HZ = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2  # 16-bit

_device_lock = threading.Lock()
_output_device_name: str = ""
_output_device_hostapi: str = ""
_output_device_index: int | None = None


def set_playback_device(
    device_name: str = "",
    device_hostapi: str = "",
    device_index: int | None = None,
) -> None:
    """Persist the render endpoint used by :func:`play_audio_file`.

    Empty name / ``None`` index means Windows default (winsound-capable).
    """
    global _output_device_name, _output_device_hostapi, _output_device_index
    with _device_lock:
        _output_device_name = str(device_name or "")
        _output_device_hostapi = str(device_hostapi or "")
        try:
            _output_device_index = (
                None if device_index is None else int(device_index))
        except (TypeError, ValueError):
            _output_device_index = None
    label = _describe_configured_output()
    log(f"[tts/play] output device set to {label}")


def configure_playback_from_ptt_settings(cfg: dict | None = None) -> None:
    """Apply Radio (PTT) output-device settings to the playback module."""
    if cfg is None:
        try:
            from ..settings import load_ptt_settings
            cfg = load_ptt_settings()
        except Exception:
            cfg = {}
    set_playback_device(
        str(cfg.get("output_device_name") or ""),
        str(cfg.get("output_device_hostapi") or ""),
        cfg.get("output_device_index"),
    )


def _configured() -> tuple[str, str, int | None]:
    with _device_lock:
        return _output_device_name, _output_device_hostapi, _output_device_index


def _describe_configured_output() -> str:
    name, hostapi, index = _configured()
    try:
        from ..ptt.recorder import describe_output_device
        return describe_output_device(name, hostapi, index)
    except Exception:
        if name:
            return name
        return "Windows default"


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

    name, hostapi, index = _configured()
    selected = bool(name) or (index is not None and int(index) >= 0)
    errors: list[str] = []

    if selected and path.suffix.lower() == ".wav":
        resolved, err = _resolve_output(name, hostapi, index)
        if resolved is None:
            log(f"[tts/play] WARNING selected output {name or index!r} "
                f"unavailable ({err or 'not found'}); falling back to "
                f"Windows default")
        else:
            label = _describe_configured_output()
            try:
                _play_sounddevice_wav(path, device=resolved, device_label=label)
                log(f"[tts/play] sounddevice OK device={label}")
                return
            except Exception as e:
                log(f"[tts/play] WARNING selected output {label} failed: {e}; "
                    f"falling back to Windows default")
                errors.append(f"sounddevice[{label}]: {e}")

    default_label = _default_output_label()

    if path.suffix.lower() == ".wav" and sys.platform == "win32":
        try:
            _play_winsound(path)
            log(f"[tts/play] winsound OK device={default_label} "
                "(Windows default; winsound cannot select a device)")
            return
        except Exception as e:
            log(f"[tts/play] winsound failed: {e}")
            errors.append(f"winsound: {e}")

    try:
        _play_pygame(path)
        log(f"[tts/play] pygame.mixer OK device={default_label} "
            "(pygame uses Windows default)")
        return
    except Exception as e:
        log(f"[tts/play] pygame.mixer failed: {e}")
        errors.append(f"pygame: {e}")

    if path.suffix.lower() == ".wav":
        try:
            _play_sounddevice_wav(path, device=None, device_label=default_label)
            log(f"[tts/play] sounddevice OK device={default_label}")
            return
        except Exception as e:
            log(f"[tts/play] sounddevice failed: {e}")
            errors.append(f"sounddevice: {e}")

    raise RuntimeError("all playback backends failed: " + "; ".join(errors))


def _resolve_output(
    name: str, hostapi: str, index: int | None,
) -> tuple[int | None, str]:
    try:
        from ..ptt.recorder import resolve_output_device
        return resolve_output_device(name, hostapi, index)
    except Exception as e:
        return None, str(e)


def _default_output_label() -> str:
    try:
        from ..ptt.recorder import describe_output_device
        return describe_output_device("", "", None)
    except Exception:
        return "Windows default"


def _play_winsound(path: Path) -> None:
    import winsound  # type: ignore[import-not-found]
    winsound.PlaySound(str(path), winsound.SND_FILENAME)


def _play_pygame(path: Path) -> None:
    from ..ptt.ffb_safe import apply_sdl_ffb_safe_hints
    apply_sdl_ffb_safe_hints()
    import pygame
    if pygame.mixer.get_init() is None:
        pygame.mixer.init()
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()
    clock = pygame.time.Clock()
    while pygame.mixer.music.get_busy():
        clock.tick(40)


def _play_sounddevice_wav(
    path: Path,
    device: int | None = None,
    device_label: str = "",
) -> None:
    import sounddevice as sd  # type: ignore
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
        width = w.getsampwidth()
        channels = w.getnchannels()
    if width != 2:
        raise RuntimeError(f"unsupported WAV sample width {width}")
    kwargs: dict = {"samplerate": rate}
    if device is not None and int(device) >= 0:
        kwargs["device"] = int(device)
    log(f"[tts/play] sounddevice opening device={device_label or device!r} "
        f"index={device} rate={rate} ch={channels}")
    try:
        import numpy as np
        data = np.frombuffer(raw, dtype=np.int16)
        if channels > 1:
            data = data.reshape(-1, channels)
        sd.play(data, **kwargs)
        sd.wait()
        return
    except ImportError:
        pass
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    sd.play(samples, **kwargs)
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
