"""Audio capture from a selectable input device.

Uses ``sounddevice`` (PortAudio) to record 16 kHz mono PCM.  Provides
device listing (input and Windows render/output endpoints), a live RMS
level meter, and WAV export for the STT step.

Bluetooth Hands-Free (HFP) capture can mute the stereo A2DP render
endpoint while the mic stream is open. The recorder therefore closes
the capture stream promptly after each utterance unless the user opts
into ``keep_warm``.
"""
from __future__ import annotations

import io
import math
import struct
import threading
import time
import wave
from dataclasses import dataclass
from ..logbook import log


SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCKSIZE = 1024
DEFAULT_MIC_GRACE_S = 0.45
_PREROLL_BLOCKS = 8  # ~0.5 s at 16 kHz / 1024

_HOSTAPI_PRIO = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}


@dataclass
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool = False
    hostapi_name: str = ""
    max_output_channels: int = 0


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
        return sd
    except (ImportError, OSError) as e:
        log(f"[ptt/recorder] sounddevice unavailable: {e}")
        return None


def _hostapi_names(sd) -> dict[int, str]:
    hostapis: dict[int, str] = {}
    try:
        for ha in sd.query_hostapis():
            hostapis[ha["index"]] = ha["name"]
    except Exception:
        pass
    return hostapis


def _dedupe_by_name(raw: list[AudioDevice]) -> list[AudioDevice]:
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
    return list(seen.values())


def _list_devices(kind: str) -> list[AudioDevice]:
    """kind is ``input`` or ``output``."""
    sd = _import_sounddevice()
    if sd is None:
        return []
    try:
        default_pair = sd.default.device
        default_idx = default_pair[0] if kind == "input" else default_pair[1]
    except Exception:
        default_idx = -1
    hostapis = _hostapi_names(sd)
    try:
        raw: list[AudioDevice] = []
        for i, d in enumerate(sd.query_devices()):
            n_in = int(d.get("max_input_channels") or 0)
            n_out = int(d.get("max_output_channels") or 0)
            if (kind == "input" and n_in <= 0) or (kind == "output" and n_out <= 0):
                continue
            ha_name = hostapis.get(d.get("hostapi", -1), "")
            raw.append(AudioDevice(
                index=i,
                name=d["name"],
                max_input_channels=n_in,
                max_output_channels=n_out,
                default_samplerate=d["default_samplerate"],
                is_default=(i == default_idx),
                hostapi_name=ha_name,
            ))
        return _dedupe_by_name(raw)
    except Exception as e:
        log(f"[ptt/recorder] {kind} device enumeration failed: {e}")
        return []


def list_input_devices() -> list[AudioDevice]:
    """Return available audio input devices, deduped per physical device.

    Prefers WASAPI and MME host APIs (lower latency / broader driver
    support on Windows). When the same device name appears under
    multiple host APIs, only the highest-priority entry is kept.
    Returns an empty list if sounddevice / PortAudio is unavailable.
    """
    return _list_devices("input")


def list_output_devices() -> list[AudioDevice]:
    """Return available Windows render / output endpoints, deduped by name.

    Same WASAPI-over-MME preference as :func:`list_input_devices`.
    """
    return _list_devices("output")


def _find_in(devices: list[AudioDevice], name: str, hostapi: str = "") -> int | None:
    if not name:
        return None
    if hostapi:
        for d in devices:
            if d.name == name and d.hostapi_name == hostapi:
                return d.index
    for d in devices:
        if d.name == name:
            return d.index
    needle = name.strip().lower()
    for d in devices:
        if needle and needle in d.name.strip().lower():
            return d.index
    return None


def find_device_by_name(name: str, hostapi: str = "") -> int | None:
    """Look up an input device index by name (and optional host API)."""
    return _find_in(list_input_devices(), name, hostapi)


def find_output_device_by_name(name: str, hostapi: str = "") -> int | None:
    """Look up an output / render device index by name."""
    return _find_in(list_output_devices(), name, hostapi)


def _resolve_device(
    devices: list[AudioDevice],
    device_name: str = "",
    device_hostapi: str = "",
    device_index: int | None = None,
    *,
    kind: str = "input",
) -> tuple[int | None, str]:
    noun = "mic" if kind == "input" else "output"
    if device_name:
        idx = _find_in(devices, device_name, device_hostapi)
        if idx is not None and idx >= 0:
            return idx, ""
        where = "Радио (PTT)"
        return None, (
            f"selected {noun} {device_name!r} not found — "
            f"open {where} and pick a device")
    if device_index is not None:
        try:
            idx = int(device_index)
        except (TypeError, ValueError):
            idx = -1
        if idx >= 0:
            if any(d.index == idx for d in devices) or not devices:
                return idx, ""
            return None, f"{noun} index {idx} not found"
    for d in devices:
        if d.is_default and d.index >= 0:
            return d.index, ""
    if devices:
        return devices[0].index, ""
    if kind == "input":
        return None, "no input device available (select a mic in PTT settings)"
    return None, f"no {noun} device available (select one in PTT settings)"


def resolve_input_device(
    device_name: str = "",
    device_hostapi: str = "",
    device_index: int | None = None,
) -> tuple[int | None, str]:
    """Resolve a PortAudio input device.

    Prefers persisted name (+ host API), then a non-negative index, then
    the PortAudio default *if it is a real device*. Never returns ``-1``
    (PortAudio's "no default" sentinel that raises
    ``Error querying device -1``).

    Returns ``(index, error)``. ``error`` is empty on success.
    """
    return _resolve_device(
        list_input_devices(), device_name, device_hostapi, device_index,
        kind="input")


def resolve_output_device(
    device_name: str = "",
    device_hostapi: str = "",
    device_index: int | None = None,
) -> tuple[int | None, str]:
    """Resolve a PortAudio render device. Never returns ``-1``.

    An empty name and ``None`` index means "use the Windows default"
    — returns ``(None, "")`` so the caller can fall back to winsound
    without treating it as an error.
    """
    if not device_name and device_index is None:
        return None, ""
    if not device_name and device_index is not None:
        try:
            if int(device_index) < 0:
                return None, ""
        except (TypeError, ValueError):
            return None, ""
    return _resolve_device(
        list_output_devices(), device_name, device_hostapi, device_index,
        kind="output")


def describe_output_device(
    device_name: str = "",
    device_hostapi: str = "",
    device_index: int | None = None,
) -> str:
    """Human-readable label for logs (selected name, else Windows default)."""
    devices = list_output_devices()
    if device_name:
        for d in devices:
            if d.name == device_name and (
                    not device_hostapi or d.hostapi_name == device_hostapi):
                ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
                return f"{d.name}{ha} index={d.index}"
        return f"{device_name} (not currently enumerated)"
    if device_index is not None:
        try:
            idx = int(device_index)
        except (TypeError, ValueError):
            idx = -1
        if idx >= 0:
            for d in devices:
                if d.index == idx:
                    ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
                    return f"{d.name}{ha} index={d.index}"
            return f"index={idx}"
    for d in devices:
        if d.is_default:
            ha = f" [{d.hostapi_name}]" if d.hostapi_name else ""
            return f"{d.name}{ha} index={d.index} (Windows default)"
    return "Windows default"


def recorder_from_ptt_settings(cfg: dict) -> AudioRecorder:
    """Build a recorder bound to the persisted UI device (name, then index)."""
    idx, err = resolve_input_device(
        str(cfg.get("device_name") or ""),
        str(cfg.get("device_hostapi") or ""),
        cfg.get("device_index"),
    )
    label = str(cfg.get("device_name") or "")
    try:
        grace = float(cfg.get("mic_grace_s", DEFAULT_MIC_GRACE_S))
    except (TypeError, ValueError):
        grace = DEFAULT_MIC_GRACE_S
    rec = AudioRecorder(
        device_index=idx,
        device_label=label,
        grace_s=grace,
        keep_warm=bool(cfg.get("mic_keep_warm", False)),
    )
    if err:
        rec.last_error = err
        log(f"[ptt/recorder] {err}")
    return rec


class AudioRecorder:
    """16 kHz mono recorder with live level metering.

    Usage::

        rec = AudioRecorder(device_index=None)  # None = system default
        rec.start()
        # ... user speaks ...
        wav_bytes = rec.stop()   # returns WAV file content as bytes
        level = rec.rms_level    # 0.0..1.0 normalised RMS level

    ``keep_warm=True`` leaves the capture stream open between utterances
    (faster short Bluetooth PTT, but HFP may mute stereo headphones).
    Default is to close the stream promptly after each ``stop()``.
    """

    def __init__(self, device_index: int | None = None,
                 device_label: str = "",
                 grace_s: float = DEFAULT_MIC_GRACE_S,
                 keep_warm: bool = False) -> None:
        self._device = device_index
        self._device_label = device_label
        self._grace_s = max(0.0, float(grace_s))
        self._keep_warm = bool(keep_warm)
        self._chunks: list[bytes] = []
        self._preroll: list[bytes] = []
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._recording = False
        self._stream = None
        self._rms: float = 0.0
        self._start_time: float = 0.0
        self._first_sample = threading.Event()
        self.last_error: str = ""

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

    def warm(self) -> bool:
        """Open the capture stream without starting an utterance.

        Bluetooth HFP stays open until :meth:`unwarm` — that can mute
        the stereo render endpoint. Default PTT does not call this.
        """
        ok = self._open_stream(for_warm=True)
        if ok:
            log(f"[ptt/recorder] mic kept warm label={self._label()!r} "
                "(HFP capture may mute Bluetooth stereo until unwarm)")
        return ok

    def unwarm(self) -> None:
        """Close a keep-warm capture stream promptly."""
        with self._lifecycle:
            self._close_stream(reason="unwarm")

    def start(self) -> bool:
        """Begin recording. Returns False if the selected device cannot open.

        Never passes PortAudio's default ``-1`` — that raises
        ``Error querying device -1`` when no default input exists
        (Bluetooth-only mics are a common case).
        """
        with self._lifecycle:
            self.last_error = ""
            self._first_sample.clear()
            with self._lock:
                self._chunks = list(self._preroll)
                self._preroll.clear()
                if self._chunks:
                    self._first_sample.set()
            self._rms = 0.0
            self._start_time = time.monotonic()
            # Arm capture before PortAudio start() so the first HFP callback
            # is not dropped while the Bluetooth stream is coming up.
            self._recording = True
            if self._stream is not None:
                log(f"[ptt/recorder] started (warm stream) device={self._label()!r} "
                    f"index={self._device}")
                return True
            if not self._open_stream_locked():
                self._recording = False
                return False
            log(f"[ptt/recorder] started device={self._label()!r} index={self._device}")
            return True

    def _open_stream(self, for_warm: bool = False) -> bool:
        with self._lifecycle:
            if self._stream is not None:
                return True
            return self._open_stream_locked()

    def _open_stream_locked(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore[import-untyped]
        except (ImportError, OSError) as e:
            self.last_error = f"sounddevice not available: {e}"
            log(f"[ptt/recorder] {self.last_error}")
            return False
        device = self._device
        if device is None or int(device) < 0:
            self.last_error = (
                "cannot open mic: no device selected and PortAudio has "
                "no default input (pick a device in Радио (PTT))")
            log(f"[ptt/recorder] start failed: {self.last_error}")
            return False
        label = self._label()
        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=int(device),
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self._audio_callback,
            )
            self._stream.start()
            return True
        except Exception as e:
            self.last_error = f"cannot open mic {label!r} (index={device}): {e}"
            log(f"[ptt/recorder] start failed: {self.last_error}")
            self._stream = None
            return False

    def _label(self) -> str:
        return self._device_label or f"index={self._device}"

    def _audio_callback(self, indata: bytes, frames: int,
                        time_info, status) -> None:
        raw = bytes(indata)
        with self._lock:
            if self._recording:
                self._chunks.append(raw)
                self._first_sample.set()
            elif self._keep_warm and self._stream is not None:
                self._preroll.append(raw)
                if len(self._preroll) > _PREROLL_BLOCKS:
                    del self._preroll[0:_PREROLL_BLOCKS // 2]
        n_samples = len(raw) // 2
        if n_samples > 0:
            samples = struct.unpack(f"<{n_samples}h", raw)
            rms = math.sqrt(sum(s * s for s in samples) / n_samples)
            self._rms = min(1.0, rms / 32768.0)

    def stop(self, grace_s: float | None = None) -> bytes:
        """Stop the utterance, wait briefly for late Bluetooth samples, close.

        ``grace_s`` defaults to the recorder's configured grace. The
        capture stream is closed promptly afterwards unless ``keep_warm``
        is set — holding HFP open can mute Bluetooth stereo playback.
        """
        grace = self._grace_s if grace_s is None else max(0.0, float(grace_s))
        with self._lifecycle:
            # Keep _recording True during the grace wait so the PortAudio
            # callback still appends chunks (Bluetooth HFP often delivers
            # the first block after stream.start() has already returned).
            if self._stream is not None and not self._first_sample.is_set() and grace > 0:
                self._recording = True
                log(f"[ptt/recorder] waiting up to {grace:.2f}s for first samples "
                    f"device={self._label()!r}")
                self._first_sample.wait(timeout=grace)
                if self._first_sample.is_set():
                    extra = min(0.12, grace)
                    if extra > 0:
                        time.sleep(extra)
            self._recording = False
            if self._keep_warm:
                log(f"[ptt/recorder] utterance ended, mic kept warm "
                    f"device={self._label()!r} "
                    "(HFP capture may mute Bluetooth stereo)")
            else:
                self._close_stream(reason="ptt-release")
            self._rms = 0.0
            with self._lock:
                raw = b"".join(self._chunks)
                self._chunks.clear()
            return _pcm_to_wav(raw, SAMPLE_RATE, CHANNELS)

    def _close_stream(self, reason: str = "") -> None:
        stream = self._stream
        self._stream = None
        self._recording = False
        with self._lock:
            self._preroll.clear()
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        why = f" reason={reason}" if reason else ""
        log(f"[ptt/recorder] closed mic promptly device={self._label()!r} "
            f"index={self._device}{why} "
            "(HFP capture can mute Bluetooth stereo until this closes)")

    def set_device(self, device_index: int | None,
                   device_label: str = "") -> None:
        self._device = device_index
        if device_label:
            self._device_label = device_label


def _pcm_to_wav(pcm: bytes, rate: int, channels: int) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()
