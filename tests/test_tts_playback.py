"""In-process TTS playback: no PowerShell windows, logged errors."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from live_telemetry_evo.tts.playback import (
    PCM_RATE_HZ,
    hidden_run,
    match_channels,
    pcm16_to_wav,
    play_audio_file,
    playback_source_is_numpy_free,
    resample_pcm16,
    run_selftest_audio,
    set_playback_device,
    _play_attempts,
    _play_sounddevice_wav,
)


def _tiny_wav(path: Path, frames: int = 240) -> Path:
    pcm = b"\x00\x00" * frames
    return pcm16_to_wav(pcm, path, rate=PCM_RATE_HZ)


class TestPcmToWav:
    def test_writes_valid_wav(self, tmp_path):
        out = tmp_path / "t.wav"
        pcm = b"\x01\x00\x02\x00" * 10
        pcm16_to_wav(pcm, out, rate=24000)
        with wave.open(str(out), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 24000
            assert w.readframes(w.getnframes()) == pcm


class TestPlayAudioFile:
    def setup_method(self):
        set_playback_device("", "", None)

    def teardown_method(self):
        set_playback_device("", "", None)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            play_audio_file(tmp_path / "nope.wav")

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.wav"
        p.write_bytes(b"")
        with pytest.raises(RuntimeError, match="empty"):
            play_audio_file(p)

    def test_uses_pygame_not_powershell(self, tmp_path):
        wav = _tiny_wav(tmp_path / "ok.wav")
        mixer = MagicMock()
        mixer.get_init.return_value = None
        mixer.music.get_busy.side_effect = [True, False]
        fake_pygame = MagicMock()
        fake_pygame.mixer = mixer

        with patch.dict(sys.modules, {"pygame": fake_pygame}), \
             patch("live_telemetry_evo.tts.playback._play_winsound",
                   side_effect=RuntimeError("skip winsound")), \
             patch("subprocess.run") as sp:
            play_audio_file(wav)

        mixer.init.assert_called()
        mixer.music.load.assert_called()
        mixer.music.play.assert_called()
        sp.assert_not_called()

    def test_raises_when_all_backends_fail(self, tmp_path):
        wav = _tiny_wav(tmp_path / "fail.wav")
        with patch("live_telemetry_evo.tts.playback._play_winsound",
                   side_effect=RuntimeError("no win")), \
             patch("live_telemetry_evo.tts.playback._play_pygame",
                   side_effect=RuntimeError("no pygame")), \
             patch("live_telemetry_evo.tts.playback._play_sounddevice_wav",
                   side_effect=RuntimeError("no sd")):
            with pytest.raises(RuntimeError, match="all playback backends failed"):
                play_audio_file(wav)

    def test_selected_device_uses_sounddevice_not_winsound(self, tmp_path):
        wav = _tiny_wav(tmp_path / "sel.wav")
        set_playback_device("Headphones (Px7 S3 Stereo)", "Windows WASAPI", 5)
        with patch("live_telemetry_evo.tts.playback._resolve_output",
                   return_value=(5, "")), \
             patch("live_telemetry_evo.tts.playback._play_sounddevice_wav") as sd, \
             patch("live_telemetry_evo.tts.playback._play_winsound") as ws, \
             patch("live_telemetry_evo.tts.playback._play_pygame") as pg:
            play_audio_file(wav)
        sd.assert_called_once()
        kwargs = sd.call_args.kwargs
        assert kwargs.get("device") == 5
        ws.assert_not_called()
        pg.assert_not_called()

    def test_missing_selected_device_falls_back_to_winsound(self, tmp_path):
        wav = _tiny_wav(tmp_path / "fb.wav")
        set_playback_device("Missing Speaker", "Windows WASAPI", 99)
        logs: list[str] = []
        with patch("live_telemetry_evo.tts.playback._resolve_output",
                   return_value=(None, "not found")), \
             patch("live_telemetry_evo.tts.playback._play_winsound") as ws, \
             patch("live_telemetry_evo.tts.playback._play_pygame",
                   side_effect=RuntimeError("skip")), \
             patch("live_telemetry_evo.tts.playback.log",
                   side_effect=lambda m: logs.append(m)), \
             patch("sys.platform", "win32"):
            play_audio_file(wav)
        ws.assert_called_once()
        joined = "\n".join(logs)
        assert "WARNING" in joined
        assert "falling back" in joined.lower() or "WARNING" in joined

    def test_source_has_no_powershell_player(self):
        from live_telemetry_evo.tts import edge_engine, elevenlabs_engine, playback
        for mod in (edge_engine, elevenlabs_engine, playback):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            assert '["powershell"' not in src
            assert "SoundPlayer" not in src


class TestHiddenRun:
    def test_sets_create_no_window_on_win32(self):
        completed = subprocess.CompletedProcess(["x"], 0)
        with patch("sys.platform", "win32"), \
             patch("subprocess.run", return_value=completed) as run, \
             patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
             patch.object(subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
             patch.object(subprocess, "STARTUPINFO",
                          MagicMock(), create=True):
            hidden_run(["echo", "x"])
            kwargs = run.call_args.kwargs
            assert kwargs.get("creationflags") == 0x08000000
            assert kwargs.get("startupinfo") is not None


class TestPcmResampleAndChannels:
    def test_resample_16000_to_48000_length(self):
        pcm = b"\x00\x10" * 16  # 16 mono frames
        out = resample_pcm16(pcm, 1, 16000, 48000)
        assert len(out) == 16 * 3 * 2

    def test_resample_same_rate_is_noop(self):
        pcm = b"\x01\x00\x02\x00"
        assert resample_pcm16(pcm, 1, 48000, 48000) == pcm

    def test_mono_to_stereo_duplicates(self):
        pcm = b"\x01\x00\x02\x00"
        out = match_channels(pcm, 1, 2)
        assert out == b"\x01\x00\x01\x00\x02\x00\x02\x00"

    def test_stereo_to_mono_averages(self):
        # frames (2, 4) -> 3
        pcm = b"\x02\x00\x04\x00"
        out = match_channels(pcm, 2, 1)
        assert out == b"\x03\x00"

    def test_play_attempts_retry_native_rate(self):
        attempts = _play_attempts(16000, 1, 48000, 2)
        assert attempts[0] == (16000, 1, "wav")
        assert (48000, 1, "native-rate") in attempts
        assert any(a[1] == 2 for a in attempts)


class _FakeRawStream:
    opens: list
    writes: list
    fail_rates: set

    def __init__(self, **kwargs):
        rate = kwargs.get("samplerate")
        if rate in self.fail_rates:
            raise RuntimeError(f"invalid sample rate {rate}")
        self.kwargs = kwargs
        type(self).opens.append(kwargs)
        self.chunks = []

    def start(self):
        return None

    def write(self, data):
        self.chunks.append(bytes(data))
        type(self).writes.append(bytes(data))

    def stop(self):
        return None

    def close(self):
        return None


class _FakeSoundDevice:
    def __init__(self, native_rate=48000, native_ch=2, fail_rates=()):
        self._native_rate = native_rate
        self._native_ch = native_ch
        rejected = set(fail_rates)

        class Stream(_FakeRawStream):
            opens = []
            writes = []
            fail_rates = rejected

        self.RawOutputStream = Stream
        self.Stream = Stream

    def query_devices(self, *args, **kwargs):
        return {
            "default_samplerate": self._native_rate,
            "max_output_channels": self._native_ch,
        }

    def get_portaudio_version(self):
        return (0, "fake")


class TestRawOutputStreamPlayback:
    def test_source_is_numpy_free(self):
        assert playback_source_is_numpy_free()

    def test_spec_still_excludes_numpy(self):
        spec = Path(__file__).parent.parent / "LiveTelemetryEvo.spec"
        text = spec.read_text(encoding="utf-8")
        assert '"numpy"' in text
        assert "RawOutputStream" in text

    def test_writes_int16_without_numpy(self, tmp_path):
        wav = _tiny_wav(tmp_path / "raw.wav", frames=32)
        fake = _FakeSoundDevice()
        real_import = __import__

        def blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "numpy" or name.startswith("numpy."):
                raise ImportError("numpy blocked")
            if name == "sounddevice":
                return fake
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", blocked):
            _play_sounddevice_wav(
                wav, device=3, device_label="Headphones (Px7 S3 Stereo)")
        assert fake.Stream.opens
        first = fake.Stream.opens[0]
        assert first["dtype"] == "int16"
        assert first["device"] == 3
        assert first["channels"] == 1
        assert sum(len(c) for c in fake.Stream.writes) > 0

    def test_retries_native_rate_when_wav_rate_rejected(self, tmp_path):
        wav = pcm16_to_wav(b"\x00\x00" * 16, tmp_path / "16k.wav", rate=16000)
        fake = _FakeSoundDevice(native_rate=48000, native_ch=2, fail_rates=(16000,))
        with patch.dict(sys.modules, {"sounddevice": fake}):
            _play_sounddevice_wav(wav, device=3, device_label="Px7")
        rates = [o["samplerate"] for o in fake.Stream.opens]
        assert 48000 in rates
        assert rates[-1] == 48000

    def test_stereo_wav_opens_with_two_channels(self, tmp_path):
        pcm = b"\x00\x00\x00\x00" * 8  # 8 stereo frames
        wav = pcm16_to_wav(pcm, tmp_path / "st.wav", rate=48000, channels=2)
        fake = _FakeSoundDevice(native_rate=48000, native_ch=2)
        with patch.dict(sys.modules, {"sounddevice": fake}):
            _play_sounddevice_wav(wav, device=1, device_label="Speakers")
        assert fake.Stream.opens[0]["channels"] == 2

    def test_selftest_audio_passes_with_fake_sounddevice(self):
        fake = _FakeSoundDevice()
        with patch.dict(sys.modules, {"sounddevice": fake}):
            rc = run_selftest_audio()
        assert rc == 0

    def test_ci_runs_frozen_selftest_audio(self):
        release = Path(__file__).parent.parent / ".github/workflows/release.yml"
        smoke = Path(__file__).parent.parent / ".github/workflows/windows-smoke.yml"
        rel = release.read_text(encoding="utf-8")
        smk = smoke.read_text(encoding="utf-8")
        assert "--selftest-audio" in rel
        assert "--selftest-audio" in smk
        assert "RawOutputStream: OK" in rel
        assert "NumPy must be installed" in rel


class TestEngineUsesPlayback:
    def test_elevenlabs_play_calls_inprocess(self, tmp_path):
        from live_telemetry_evo.tts.elevenlabs_engine import ElevenLabsEngine
        engine = ElevenLabsEngine()
        wav = _tiny_wav(tmp_path / "el.wav")
        with patch("live_telemetry_evo.tts.elevenlabs_engine.play_audio_file") as play:
            engine._play(wav)
            play.assert_called_once()

    def test_elevenlabs_play_reraises(self, tmp_path):
        from live_telemetry_evo.tts.elevenlabs_engine import ElevenLabsEngine
        engine = ElevenLabsEngine()
        wav = _tiny_wav(tmp_path / "el.wav")
        with patch("live_telemetry_evo.tts.elevenlabs_engine.play_audio_file",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                engine._play(wav)

    def test_edge_play_calls_inprocess(self, tmp_path):
        from live_telemetry_evo.tts.edge_engine import EdgeTTSEngine
        engine = EdgeTTSEngine()
        wav = _tiny_wav(tmp_path / "edge.wav")
        with patch("live_telemetry_evo.tts.edge_engine.play_audio_file") as play:
            engine._play(wav)
            play.assert_called_once()
