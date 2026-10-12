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
    pcm16_to_wav,
    play_audio_file,
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
