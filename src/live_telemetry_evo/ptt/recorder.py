"""Audio capture from a selectable input device.

Uses ``sounddevice`` (PortAudio) to record 16 kHz mono PCM.  Provides
device listing, a live RMS level meter, and WAV export for the STT step.
"""
from __future__ import annotations

import io
import math
import struct
import threading
import time
import wave
from dataclasses import dataclass
from typing import Optional

from ..logbook import log


SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 1024


@dataclass
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool = False
    hostapi_name: str = ""


def list_input_devices() -> list[AudioDevice]:
    """Return available audio input devices, deduped per physical device.

    Prefers WASAPI and MME host APIs (lower latency / broader driver
    support on Windows). When the same device name appears under
    multiple host APIs, only the highest-priority entry is kept.
    Returns an empty list if sounddevice / PortAudio is unavailable.
    """
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except (ImportError, OSError) as e:
        log(f"[ptt/recorder] sounddevice unavailable: {e}")
        return []
    devices: list[AudioDevice] = []
    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = -1
    hostapis: dict[int, str] = {}
    try:
        for ha in sd.query_hostapis():
            hostapis[ha["index"]] = ha["name"]
    except Exception:
        pass
    _HOSTAPI_PRIO = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
    try:
        raw: list[AudioDevice] = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                ha_name = hostapis.get(d.get("hostapi", -1), "")
                raw.append(AudioDevice(
                    index=i,
                    name=d["name"],
                    max_input_channels=d["max_input_channels"],
                    default_samplerate=d["default_samplerate"],
                    is_default=(i == default_input),
                    hostapi_name=ha_name,
                ))
        seen: dict[str, AudioDevice] = {}
        for dev in raw:
            key = dev.name.strip()
            if key in seen:
                old_prio = _HOSTAPI_PRIO.get(seen[key].hostapi_name, 99)
                new_prio = _HOSTAPI_PRIO.get(dev.hostapi_name, 99)
                if new_prio < old_prio:
                    seen[key] = dev
                elif dev.is_default:
                    seen[key] = dev
            else:
                seen[key] = dev
        devices = list(seen.values())
    except Exception as e:
        log(f"[ptt/recorder] device enumeration failed: {e}")
    return devices


def find_device_by_name(name: str, hostapi: str = "") -> int | None:
    """Look up a device index by name (and optional host API). Returns
    ``None`` if no match is found."""
    for d in list_input_devices():
        if d.name == name:
            if hostapi and d.hostapi_name != hostapi:
                continue
            return d.index
    return None


class AudioRecorder:
    """16 kHz mono recorder with live level metering.

    Usage::

        rec = AudioRecorder(device_index=None)  # None = system default
        rec.start()
        # ... user speaks ...
        wav_bytes = rec.stop()   # returns WAV file content as bytes
        level = rec.rms_level    # 0.0..1.0 normalised RMS level
    """

    def __init__(self, device_index: int | None = None) -> None:
        self._device = device_index
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._recording = False
        self._stream = None
        self._rms: float = 0.0
        self._start_time: float = 0.0

    @property
    def rms_level(self) -> float:
        """Normalised RMS level 0.0 .. 1.0 (updated per block while recording)."""
        return self._rms

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    def start(self) -> bool:
        """Begin recording. Returns False if sounddevice unavailable."""
        try:
            import sounddevice as sd  # type: ignore[import-untyped]
        except (ImportError, OSError):
            log("[ptt/recorder] sounddevice not available")
            return False
        with self._lock:
            self._chunks.clear()
        self._rms = 0.0
        self._start_time = time.monotonic()
        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=self._device,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self._audio_callback,
            )
            self._stream.start()
            self._recording = True
            return True
        except Exception as e:
            log(f"[ptt/recorder] start failed: {e}")
            return False

    def _audio_callback(self, indata: bytes, frames: int,
                        time_info, status) -> None:
        raw = bytes(indata)
        with self._lock:
            self._chunks.append(raw)
        n_samples = len(raw) // 2
        if n_samples > 0:
            samples = struct.unpack(f"<{n_samples}h", raw)
            rms = math.sqrt(sum(s * s for s in samples) / n_samples)
            self._rms = min(1.0, rms / 32768.0)

    def stop(self) -> bytes:
        """Stop recording and return the captured audio as a WAV byte string."""
        self._recording = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._rms = 0.0
        with self._lock:
            raw = b"".join(self._chunks)
            self._chunks.clear()
        return _pcm_to_wav(raw, SAMPLE_RATE, CHANNELS)

    def set_device(self, device_index: int | None) -> None:
        self._device = device_index


def _pcm_to_wav(pcm: bytes, rate: int, channels: int) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()
