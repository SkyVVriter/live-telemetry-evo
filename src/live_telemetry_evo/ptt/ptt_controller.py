"""PTT controller: orchestrates recording, STT, intent dispatch.

Manages the push-to-talk lifecycle:
- Listens for the bound control (keyboard or joystick)
- Records audio while PTT is active
- Runs STT on the captured audio
- Dispatches intents (local commands or driver_feedback)
- Plays start/stop beeps
- Mutes TTS output while PTT is active
"""
from __future__ import annotations

import struct
import threading
import time
from enum import Enum
from typing import Callable, Optional

from ..logbook import log
from .input_binding import InputListener, PTTBinding
from .recorder import AudioRecorder


class PTTMode(Enum):
    HOLD = "hold"
    TOGGLE = "toggle"


_BEEP_DURATION_S = 0.05
_BEEP_FREQ_START = 880
_BEEP_FREQ_STOP = 660
_MIN_HOLD_DURATION_S = 0.15


def _generate_beep(freq: int, duration: float = _BEEP_DURATION_S,
                   sample_rate: int = 16000) -> bytes:
    """Generate a short sine-wave beep as 16-bit PCM."""
    import math
    n_samples = int(sample_rate * duration)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        fade = 1.0
        if i < 200:
            fade = i / 200
        elif i > n_samples - 200:
            fade = (n_samples - i) / 200
        val = int(16000 * fade * math.sin(2 * math.pi * freq * t))
        samples.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{len(samples)}h", *samples)


class PTTController:
    """Orchestrates the full PTT lifecycle.

    Parameters:
        recorder: AudioRecorder instance.
        on_audio_captured: Called with (wav_bytes, duration_s) when
            recording finishes.
        on_mute_tts: Called with True when PTT starts (mute TTS),
            False when PTT ends (unmute).
        mode: Hold or toggle mode.
        max_duration_s: Maximum recording duration before auto-stop.
    """

    def __init__(
        self,
        recorder: AudioRecorder | None = None,
        on_audio_captured: Callable[[bytes, float], None] | None = None,
        on_mute_tts: Callable[[bool], None] | None = None,
        mode: PTTMode = PTTMode.HOLD,
        max_duration_s: float = 15.0,
    ) -> None:
        self.recorder = recorder or AudioRecorder()
        self.on_audio_captured = on_audio_captured
        self.on_mute_tts = on_mute_tts
        self.mode = mode
        self.max_duration_s = max_duration_s
        self._input = InputListener()
        self._active = False
        self._press_time: float = 0.0
        self._timeout_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    @property
    def binding(self) -> Optional[PTTBinding]:
        return self._input.binding

    @property
    def is_active(self) -> bool:
        return self._active

    def set_binding(self, binding: Optional[PTTBinding]) -> None:
        self._input.set_binding(binding)

    def set_mode(self, mode: PTTMode) -> None:
        self.mode = mode

    def start(self) -> None:
        """Start listening for PTT input."""
        self._input.start(
            on_press=self._on_press,
            on_release=self._on_release,
        )

    def stop(self) -> None:
        """Stop listening and cancel any active recording."""
        self._input.stop()
        if self._active:
            self._stop_recording(cancelled=True)

    def start_assign(self, callback: Callable[[PTTBinding], None]) -> None:
        """Enter assign mode."""
        self._input.start_assign(callback)

    def stop_assign(self) -> None:
        self._input.stop_assign()

    def _on_press(self) -> None:
        with self._lock:
            if self.mode == PTTMode.HOLD:
                self._start_recording()
            elif self.mode == PTTMode.TOGGLE:
                if self._active:
                    self._stop_recording()
                else:
                    self._start_recording()

    def _on_release(self) -> None:
        with self._lock:
            if self.mode == PTTMode.HOLD and self._active:
                elapsed = time.monotonic() - self._press_time
                if elapsed < _MIN_HOLD_DURATION_S:
                    log(f"[ptt] press too short ({elapsed:.0f}ms), ignoring")
                    self._stop_recording(cancelled=True)
                else:
                    self._stop_recording()

    def _start_recording(self) -> None:
        if self._active:
            return
        self._active = True
        self._press_time = time.monotonic()

        if self.on_mute_tts:
            self.on_mute_tts(True)

        _play_beep_async(_BEEP_FREQ_START)

        ok = self.recorder.start()
        if not ok:
            log("[ptt] recorder failed to start")
            self._active = False
            if self.on_mute_tts:
                self.on_mute_tts(False)
            return

        if self.max_duration_s > 0:
            self._timeout_timer = threading.Timer(
                self.max_duration_s, self._on_timeout)
            self._timeout_timer.daemon = True
            self._timeout_timer.start()

        log("[ptt] recording started")

    def _stop_recording(self, cancelled: bool = False) -> None:
        if not self._active:
            return
        self._active = False

        if self._timeout_timer is not None:
            self._timeout_timer.cancel()
            self._timeout_timer = None

        wav_data = self.recorder.stop()
        duration = time.monotonic() - self._press_time

        if self.on_mute_tts:
            self.on_mute_tts(False)

        if not cancelled and self.on_audio_captured and len(wav_data) > 44:
            _play_beep_async(_BEEP_FREQ_STOP)
            log(f"[ptt] recording stopped ({duration:.1f}s)")
            self.on_audio_captured(wav_data, duration)
        else:
            if cancelled:
                log("[ptt] recording cancelled")
            _play_beep_async(_BEEP_FREQ_STOP)

    def _on_timeout(self) -> None:
        with self._lock:
            if self._active:
                log(f"[ptt] max duration {self.max_duration_s}s reached")
                self._stop_recording()


def _play_beep_async(freq: int) -> None:
    """Play a short beep in a background thread (non-blocking)."""
    def _do():
        try:
            pcm = _generate_beep(freq)
            import io, wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            _play_wav_bytes(buf.getvalue())
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


def _play_wav_bytes(wav_bytes: bytes) -> None:
    """Best-effort WAV playback."""
    import sys
    import tempfile
    import subprocess
    from pathlib import Path
    tmp = Path(tempfile.gettempdir()) / "lte_ptt_beep.wav"
    tmp.write_bytes(wav_bytes)
    try:
        if sys.platform == "win32":
            import winsound  # type: ignore[import-error]
            winsound.PlaySound(str(tmp), winsound.SND_FILENAME)
        else:
            for cmd in (["aplay", str(tmp)], ["ffplay", "-nodisp", "-autoexit", str(tmp)]):
                try:
                    subprocess.run(cmd, timeout=3, capture_output=True, check=True)
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
    except Exception:
        pass
