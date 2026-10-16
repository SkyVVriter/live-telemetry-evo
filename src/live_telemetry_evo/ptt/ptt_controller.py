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
from .recorder import DEFAULT_MIC_GRACE_S, AudioRecorder


class PTTMode(Enum):
    HOLD = "hold"
    TOGGLE = "toggle"


_BEEP_DURATION_S = 0.05
_BEEP_FREQ_START = 880
_BEEP_FREQ_STOP = 660
_MIN_HOLD_DURATION_S = 0.15
_BOUNCE_DEBOUNCE_S = 0.35


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
        mic_grace_s: Seconds to wait after PTT release for late
            Bluetooth samples before closing the mic.
        mic_keep_warm: If True, leave the capture stream open between
            utterances (faster short PTT; HFP may mute stereo).
    """

    def __init__(
        self,
        recorder: AudioRecorder | None = None,
        on_audio_captured: Callable[[bytes, float], None] | None = None,
        on_mute_tts: Callable[[bool], None] | None = None,
        mode: PTTMode = PTTMode.HOLD,
        max_duration_s: float = 15.0,
        wheel_backend: str | None = None,
        on_held: Callable[[bool], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        mic_grace_s: float = DEFAULT_MIC_GRACE_S,
        mic_keep_warm: bool = False,
    ) -> None:
        self.recorder = recorder or AudioRecorder()
        self.on_audio_captured = on_audio_captured
        self.on_mute_tts = on_mute_tts
        self.mode = mode
        self.max_duration_s = max_duration_s
        self.on_held = on_held
        self.on_error = on_error
        self.mic_grace_s = max(0.0, float(mic_grace_s))
        self.mic_keep_warm = bool(mic_keep_warm)
        if hasattr(self.recorder, "_grace_s"):
            self.recorder._grace_s = self.mic_grace_s
        if hasattr(self.recorder, "_keep_warm"):
            self.recorder._keep_warm = self.mic_keep_warm
        self._input = InputListener(wheel_backend=wheel_backend)
        self._active = False
        self._stopping = False
        self._press_time: float = 0.0
        self._last_stop_time: float = 0.0
        self._timeout_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._mic_ready = threading.Event()
        self._mic_ready.set()
        self.last_error: str = ""

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
        """Start listening for PTT input (independent of iRacing)."""
        log("[ptt] listener starting (runs with or without iRacing)")
        if self.mic_keep_warm:
            try:
                self.recorder.warm()
            except Exception as e:
                log(f"[ptt] mic warm failed: {e}")
        self._input.start(
            on_press=self._on_press,
            on_release=self._on_release,
        )

    def stop(self) -> None:
        """Stop listening and cancel any active recording."""
        self._input.stop()
        if self._active:
            self._stop_recording(cancelled=True)
        if self.mic_keep_warm:
            try:
                self.recorder.unwarm()
            except Exception:
                pass

    def start_assign(self, callback: Callable[[PTTBinding], None]) -> None:
        """Enter assign mode."""
        self._input.start_assign(callback)

    def stop_assign(self) -> None:
        self._input.stop_assign()

    def _on_press(self) -> None:
        start_now = False
        stop_toggle = False
        with self._lock:
            now = time.monotonic()
            if (now - self._last_stop_time) < _BOUNCE_DEBOUNCE_S:
                log("[ptt] bounce ignored")
                return
            if self.mode == PTTMode.HOLD:
                if self._active or self._stopping:
                    return
                self._active = True
                self._press_time = now
                self._mic_ready.clear()
                start_now = True
            elif self.mode == PTTMode.TOGGLE:
                if self._stopping:
                    return
                if self._active:
                    stop_toggle = True
                else:
                    self._active = True
                    self._press_time = now
                    self._mic_ready.clear()
                    start_now = True
        if stop_toggle:
            self._stop_recording()
            return
        if start_now:
            self._begin_recording()

    def _on_release(self) -> None:
        do_stop = False
        cancelled = False
        elapsed_s = 0.0
        with self._lock:
            if self.mode == PTTMode.HOLD and self._active:
                elapsed_s = time.monotonic() - self._press_time
                cancelled = elapsed_s < _MIN_HOLD_DURATION_S
                do_stop = True
        if do_stop:
            if cancelled:
                log(f"[ptt] press too short ({elapsed_s * 1000:.0f}ms), ignoring")
            self._stop_recording(cancelled=cancelled)

    def _begin_recording(self) -> None:
        """Open the mic *without* holding ``_lock`` (Bluetooth HFP can block)."""
        err = ""
        ok = False
        try:
            if self.on_mute_tts:
                self.on_mute_tts(True)

            _play_beep_async(_BEEP_FREQ_START)

            if self.on_held:
                try:
                    self.on_held(True)
                except Exception:
                    pass

            ok = self.recorder.start()
            with self._lock:
                still_held = self._active
                if not ok:
                    err = (getattr(self.recorder, "last_error", "")
                           or "recorder failed to start")
                    self.last_error = err
                    log(f"[ptt] recorder start failed: {err}")
                    self._active = False
                elif still_held and self.max_duration_s > 0:
                    self._timeout_timer = threading.Timer(
                        self.max_duration_s, self._on_timeout)
                    self._timeout_timer.daemon = True
                    self._timeout_timer.start()
                    log("[ptt] recording started")
        finally:
            self._mic_ready.set()

        if not ok:
            if self.on_held:
                try:
                    self.on_held(False)
                except Exception:
                    pass
            if self.on_error:
                try:
                    self.on_error(err)
                except Exception:
                    pass
            if self.on_mute_tts:
                self.on_mute_tts(False)
            return
        # If the user already released, `_on_release` is waiting on
        # `_mic_ready` and will harvest; do not stop twice.

    def _stop_recording(self, cancelled: bool = False) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._active = False
            self._last_stop_time = time.monotonic()
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()
                self._timeout_timer = None
            press_time = self._press_time

        self._mic_ready.wait(timeout=8.0)
        wav_data = self.recorder.stop(grace_s=self.mic_grace_s)
        duration = time.monotonic() - press_time if press_time else 0.0
        with self._lock:
            self._stopping = False

        if self.on_held:
            try:
                self.on_held(False)
            except Exception:
                pass

        if self.on_mute_tts:
            self.on_mute_tts(False)

        if not cancelled and self.on_audio_captured and len(wav_data) > 44:
            _play_beep_async(_BEEP_FREQ_STOP)
            log(f"[ptt] recording stopped ({duration:.1f}s, {len(wav_data)} bytes)")
            self.on_audio_captured(wav_data, duration)
        else:
            if cancelled:
                log("[ptt] recording cancelled")
            elif len(wav_data) <= 44:
                err = "recording empty (mic produced no samples)"
                self.last_error = err
                log(f"[ptt] {err}")
                if self.on_error:
                    try:
                        self.on_error(err)
                    except Exception:
                        pass
            _play_beep_async(_BEEP_FREQ_STOP)

    def _on_timeout(self) -> None:
        with self._lock:
            if not self._active or self._stopping:
                return
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
    """Best-effort WAV playback through the selected TTS output device."""
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.gettempdir()) / "lte_ptt_beep.wav"
    tmp.write_bytes(wav_bytes)
    try:
        from ..tts.playback import play_audio_file
        play_audio_file(tmp)
    except Exception:
        try:
            import sys
            if sys.platform == "win32":
                import winsound  # type: ignore[import-error]
                winsound.PlaySound(str(tmp), winsound.SND_FILENAME)
        except Exception:
            pass
