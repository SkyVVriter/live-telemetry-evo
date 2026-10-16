"""In-process TTS playback. Never opens a PowerShell / console window.

Backends, in order:
1. ``sounddevice.RawOutputStream`` to a *selected* Windows render
   endpoint (when the Radio (PTT) output device is set). Writes raw
   int16 bytes — **numpy is not required** (the frozen exe excludes it;
   the high-level play/rec helpers would raise ``NumPy must be installed``).
2. ``winsound`` for WAV on Windows (stdlib, no extra DLL) — Windows
   default endpoint only. Skipped when a device is selected.
3. ``pygame.mixer`` for MP3/WAV (bundled in the frozen exe) — default
   endpoint.
4. ``sounddevice.RawOutputStream`` on the default endpoint.

WASAPI shared mode often rejects non-native rates (e.g. 16 kHz beeps
against a 48 kHz headset). If the WAV rate fails, PCM is linearly
resampled to the device's ``default_samplerate`` and retried. WAV
channel count is used first; if that is rejected, frames are duplicated
or mixed to the device's ``max_output_channels``.

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


def resample_pcm16(raw: bytes, channels: int, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolate little-endian PCM16. No numpy."""
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"invalid rate src={src_rate} dst={dst_rate}")
    channels = max(1, int(channels))
    if src_rate == dst_rate:
        return raw
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1 and len(samples) % channels:
        samples = samples[: len(samples) - (len(samples) % channels)]
    n_src = len(samples) // channels
    if n_src <= 0:
        return b""
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    out = array.array("h")
    last = n_src - 1
    scale = last / (n_dst - 1) if n_dst > 1 else 0.0
    for i in range(n_dst):
        pos = i * scale
        i0 = int(pos)
        frac = pos - i0
        i1 = i0 + 1 if i0 < last else i0
        for c in range(channels):
            a = samples[i0 * channels + c]
            b = samples[i1 * channels + c]
            out.append(int(a + (b - a) * frac))
    return out.tobytes()


def match_channels(raw: bytes, src_ch: int, dst_ch: int) -> bytes:
    """Duplicate or mix PCM16 frames so ``src_ch`` becomes ``dst_ch``."""
    src_ch = max(1, int(src_ch))
    dst_ch = max(1, int(dst_ch))
    if src_ch == dst_ch:
        return raw
    import array
    samples = array.array("h")
    samples.frombytes(raw)
    if len(samples) % src_ch:
        samples = samples[: len(samples) - (len(samples) % src_ch)]
    n_frames = len(samples) // src_ch
    out = array.array("h")
    if src_ch == 1:
        for i in range(n_frames):
            s = samples[i]
            for _ in range(dst_ch):
                out.append(s)
        return out.tobytes()
    if dst_ch == 1:
        for i in range(n_frames):
            acc = 0
            base = i * src_ch
            for c in range(src_ch):
                acc += samples[base + c]
            out.append(int(acc / src_ch))
        return out.tobytes()
    for i in range(n_frames):
        base = i * src_ch
        for c in range(dst_ch):
            out.append(samples[base + c] if c < src_ch else samples[base + src_ch - 1])
    return out.tobytes()


def _query_output_caps(sd, device: int | None) -> tuple[int | None, int | None]:
    """Return ``(default_samplerate, max_output_channels)`` or Nones."""
    try:
        if device is not None and int(device) >= 0:
            info = sd.query_devices(int(device))
        else:
            info = sd.query_devices(kind="output")
        rate = int(float(info.get("default_samplerate") or 0)) or None
        ch = int(info.get("max_output_channels") or 0) or None
        return rate, ch
    except Exception as e:
        log(f"[tts/play] could not query output caps: {e}")
        return None, None


def _write_raw_output_stream(
    sd,
    pcm: bytes,
    *,
    rate: int,
    channels: int,
    device: int | None = None,
) -> None:
    """Blocking int16 write via ``RawOutputStream``. Never calls ``sd.play``."""
    kwargs: dict = {
        "samplerate": int(rate),
        "channels": int(channels),
        "dtype": "int16",
    }
    if device is not None and int(device) >= 0:
        kwargs["device"] = int(device)
    stream = sd.RawOutputStream(**kwargs)
    try:
        stream.start()
        # PortAudio wants a contiguous buffer; write in ~50 ms chunks.
        frame_bytes = max(1, int(channels) * 2)
        chunk = frame_bytes * max(1, int(rate * 0.05))
        mv = memoryview(pcm)
        off = 0
        while off < len(pcm):
            end = min(off + chunk, len(pcm))
            # Align to a whole frame so WASAPI does not reject the write.
            aligned = end - ((end - off) % frame_bytes)
            if aligned <= off:
                aligned = end
            stream.write(bytes(mv[off:aligned]))
            off = aligned
        stream.stop()
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _play_attempts(
    wav_rate: int, wav_ch: int, native_rate: int | None, native_ch: int | None,
) -> list[tuple[int, int, str]]:
    """Ordered (rate, channels, why) attempts; first is the WAV as-is."""
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []

    def add(rate: int, ch: int, why: str) -> None:
        key = (int(rate), int(ch))
        if key in seen or rate <= 0 or ch <= 0:
            return
        seen.add(key)
        out.append((key[0], key[1], why))

    add(wav_rate, wav_ch, "wav")
    if native_rate and native_rate != wav_rate:
        add(native_rate, wav_ch, "native-rate")
    if native_ch and native_ch != wav_ch:
        add(native_rate or wav_rate, native_ch, "device-channels")
        if native_rate and native_rate != wav_rate:
            add(native_rate, native_ch, "native-rate+device-channels")
    return out


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
    native_rate, native_ch = _query_output_caps(sd, device)
    log(f"[tts/play] sounddevice opening device={device_label or device!r} "
        f"index={device} wav_rate={rate} wav_ch={channels} "
        f"native_rate={native_rate} native_ch={native_ch} "
        f"backend=RawOutputStream numpy=no")
    errors: list[str] = []
    for try_rate, try_ch, why in _play_attempts(rate, channels, native_rate, native_ch):
        pcm = raw
        try:
            if try_ch != channels:
                pcm = match_channels(pcm, channels, try_ch)
            if try_rate != rate:
                pcm = resample_pcm16(pcm, try_ch, rate, try_rate)
            log(f"[tts/play] RawOutputStream try why={why} rate={try_rate} "
                f"ch={try_ch} bytes={len(pcm)}")
            _write_raw_output_stream(
                sd, pcm, rate=try_rate, channels=try_ch, device=device)
            log(f"[tts/play] sounddevice RawOutputStream OK "
                f"device={device_label or device!r} index={device} "
                f"rate={try_rate} ch={try_ch} why={why}")
            return
        except Exception as e:
            log(f"[tts/play] RawOutputStream try why={why} rate={try_rate} "
                f"ch={try_ch} failed: {e}")
            errors.append(f"{why} {try_rate}Hz/{try_ch}ch: {e}")
    raise RuntimeError(
        "RawOutputStream failed: " + ("; ".join(errors) or "no attempts"))


def playback_source_is_numpy_free(src: str | None = None) -> bool:
    """True when the RawOutputStream play helpers never import numpy.

    Frozen onefile builds often have no ``.py`` next to the bytecode, so
    the default path inspects ``co_names`` instead of reading the source.
    """
    if src is None:
        try:
            src = Path(__file__).read_text(encoding="utf-8")
        except Exception:
            src = ""
    if src:
        if "RawOutputStream" not in src:
            return False
        start = src.find("def _write_raw_output_stream")
        end = src.find("def playback_source_is_numpy_free")
        body = src[start:end] if start >= 0 and end > start else src
        if "import numpy" in body or "from numpy" in body:
            return False
        if "sd.play(" in body or "sounddevice.play(" in body:
            return False
        return True
    names = set(_write_raw_output_stream.__code__.co_names) | set(
        _play_sounddevice_wav.__code__.co_names)
    if "numpy" in names:
        return False
    return "RawOutputStream" in names


def run_selftest_audio() -> int:
    """Prove the frozen playback path imports without numpy.

    Never requires a working speaker. Returns 0 on success, 1 on failure.
    Log lines (grep these in ``session.log``):

    * ``[selftest-audio] sounddevice.RawOutputStream: OK``
    * ``[selftest-audio] numpy not required: OK``
    * ``[selftest-audio] overall: PASS``

    Does not open a render stream (WASAPI can block forever on CI).
    """
    try:
        return _run_selftest_audio_body()
    except Exception as e:
        try:
            log(f"[selftest-audio] FAIL ({type(e).__name__}: {e})")
            log("[selftest-audio] overall: FAIL")
        except Exception:
            pass
        return 1


def _run_selftest_audio_body() -> int:
    results: dict[str, bool] = {}

    def _p(msg: str) -> None:
        print(msg)
        log(f"[selftest-audio] {msg}")

    _p("=== LiveTelemetryEvo audio self-test ===")

    no_numpy = playback_source_is_numpy_free()
    _p("numpy not required: " + (
        "OK" if no_numpy else "FAIL (playback.py uses numpy or high-level play)"))
    results["numpy_free"] = no_numpy

    try:
        import sounddevice as sd  # type: ignore
        has_raw = hasattr(sd, "RawOutputStream")
        _p(f"sounddevice.RawOutputStream: {'OK' if has_raw else 'FAIL missing'}")
        results["raw_output"] = bool(has_raw)
        try:
            ver = sd.get_portaudio_version()[1]
            _p(f"PortAudio: {ver}")
        except Exception:
            pass
    except (ImportError, OSError) as e:
        _p(f"sounddevice.RawOutputStream: FAIL ({e})")
        results["raw_output"] = False
        sd = None  # type: ignore

    try:
        import importlib
        importlib.import_module("numpy")
        _p("numpy present in this process (unused by playback)")
    except ImportError:
        _p("numpy: not installed (expected in frozen exe)")

    pcm = b"\x00\x00" * 16
    stereo = match_channels(pcm, 1, 2)
    results["match_channels"] = (len(stereo) == 64)
    _p("match_channels 1->2: " + ("OK" if results["match_channels"] else "FAIL"))
    up = resample_pcm16(pcm, 1, 16000, 48000)
    # 16 frames * 48000/16000 = 48 frames * 2 bytes
    results["resample"] = (len(up) == 96)
    _p("resample 16000->48000: " + ("OK" if results["resample"] else "FAIL"))
    # Do not open a PortAudio stream here: WASAPI on a headless runner
    # can block in C (GIL held) and freeze the process past the watchdog.

    all_ok = all(results.values())
    _p(f"Results: {results}")
    _p(f"overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


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
