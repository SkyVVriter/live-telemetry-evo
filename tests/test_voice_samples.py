"""Tests for tools/voice_samples.py with mocked subprocess calls."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Import the tool's functions directly
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import voice_samples


class TestLoadManifest:
    def test_load_json(self, tmp_path):
        manifest = {
            "profiles": [{
                "name": "TestVoice",
                "sources": [{"file": "/tmp/test.wav", "segments": [[0, 10]]}],
            }]
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = voice_samples._load_manifest(path)
        assert result["profiles"][0]["name"] == "TestVoice"

    def test_load_yaml_fallback_to_json(self, tmp_path):
        manifest = {"profiles": [{"name": "Test"}]}
        path = tmp_path / "manifest.yaml"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = voice_samples._load_manifest(path)
        assert result["profiles"][0]["name"] == "Test"


class TestSafeName:
    def test_alphanumeric(self):
        assert voice_samples._safe_name("MyVoice123") == "MyVoice123"

    def test_special_chars(self):
        result = voice_samples._safe_name("my/voice:test")
        assert "/" not in result
        assert ":" not in result

    def test_spaces_preserved(self):
        assert voice_samples._safe_name("My Voice") == "My Voice"


class TestDownloadAudio:
    @patch("voice_samples._run")
    def test_download_calls_ytdlp(self, mock_run, tmp_path):
        wav = tmp_path / "test123.wav"
        wav.write_bytes(b"\x00" * 100)

        voice_samples.download_audio("https://youtube.com/watch?v=test", tmp_path)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "yt-dlp"
        assert "--extract-audio" in args
        assert "--audio-format" in args

    @patch("voice_samples._run")
    def test_download_raises_on_no_output(self, mock_run, tmp_path):
        with pytest.raises(FileNotFoundError):
            voice_samples.download_audio("https://youtube.com/watch?v=x", tmp_path)


class TestCutSegment:
    @patch("voice_samples._run")
    def test_cut_calls_ffmpeg(self, mock_run, tmp_path):
        inp = tmp_path / "input.wav"
        out = tmp_path / "output.wav"
        inp.write_bytes(b"\x00")

        voice_samples.cut_segment(inp, 10.0, 25.5, out)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "ffmpeg"
        assert "-ss" in args
        assert "10.000" in args
        assert "-t" in args
        assert "15.500" in args


class TestNormalizeLoudness:
    @patch("voice_samples._run")
    def test_normalize_calls_ffmpeg_loudnorm(self, mock_run, tmp_path):
        inp = tmp_path / "input.wav"
        out = tmp_path / "output.wav"
        inp.write_bytes(b"\x00")

        voice_samples.normalize_loudness(inp, out, target_lufs=-16.0)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "ffmpeg"
        af_idx = args.index("-af")
        assert "loudnorm" in args[af_idx + 1]
        assert "-16" in args[af_idx + 1]

    @patch("voice_samples._run")
    def test_normalize_sets_sample_rate(self, mock_run, tmp_path):
        inp = tmp_path / "in.wav"
        out = tmp_path / "out.wav"
        inp.write_bytes(b"\x00")

        voice_samples.normalize_loudness(inp, out)

        args = mock_run.call_args[0][0]
        ar_idx = args.index("-ar")
        assert args[ar_idx + 1] == "44100"
        ac_idx = args.index("-ac")
        assert args[ac_idx + 1] == "1"


class TestNoiseReduction:
    @patch("voice_samples._run")
    def test_noise_reduction_calls_ffmpeg(self, mock_run, tmp_path):
        inp = tmp_path / "in.wav"
        out = tmp_path / "out.wav"
        inp.write_bytes(b"\x00")

        voice_samples.apply_noise_reduction(inp, out)

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "ffmpeg"
        af_idx = args.index("-af")
        assert "highpass" in args[af_idx + 1]
        assert "afftdn" in args[af_idx + 1]


class TestConvertToFormat:
    @patch("voice_samples._run")
    def test_convert_wav(self, mock_run, tmp_path):
        inp = tmp_path / "in.wav"
        out = tmp_path / "out.wav"
        inp.write_bytes(b"\x00")

        voice_samples.convert_to_format(inp, out, fmt="wav")

        args = mock_run.call_args[0][0]
        assert "pcm_s16le" in args

    @patch("voice_samples._run")
    def test_convert_mp3(self, mock_run, tmp_path):
        inp = tmp_path / "in.wav"
        out = tmp_path / "out.mp3"
        inp.write_bytes(b"\x00")

        voice_samples.convert_to_format(inp, out, fmt="mp3")

        args = mock_run.call_args[0][0]
        assert "libmp3lame" in args


class TestGetDuration:
    @patch("voice_samples._run")
    def test_get_duration(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(stdout="42.500\n")
        path = tmp_path / "test.wav"
        path.write_bytes(b"\x00")

        dur = voice_samples.get_duration(path)

        assert dur == pytest.approx(42.5)
        args = mock_run.call_args[0][0]
        assert args[0] == "ffprobe"


class TestProcessProfile:
    @patch("voice_samples.get_duration", return_value=15.0)
    @patch("voice_samples.convert_to_format")
    @patch("voice_samples.normalize_loudness")
    @patch("voice_samples.cut_segment")
    def test_local_file_pipeline(self, mock_cut, mock_norm, mock_conv,
                                  mock_dur, tmp_path):
        src_file = tmp_path / "source.wav"
        src_file.write_bytes(b"\x00" * 1000)

        mock_conv.side_effect = lambda i, o, **kw: o.write_bytes(b"\x00")

        profile = {
            "name": "TestProfile",
            "sources": [
                {"file": str(src_file), "segments": [[0, 15], [20, 35]]},
            ],
        }
        report = voice_samples.process_profile(profile, tmp_path / "out")

        assert report["name"] == "TestProfile"
        assert report["segments"] == 2
        assert report["total_duration_s"] == pytest.approx(30.0)
        assert len(report["errors"]) == 0
        assert mock_cut.call_count == 2
        assert mock_norm.call_count == 2
        assert mock_conv.call_count == 2

    @patch("voice_samples.get_duration", return_value=10.0)
    @patch("voice_samples.convert_to_format")
    @patch("voice_samples.normalize_loudness")
    @patch("voice_samples.apply_noise_reduction")
    @patch("voice_samples.cut_segment")
    def test_noise_reduction_enabled(self, mock_cut, mock_nr, mock_norm,
                                      mock_conv, mock_dur, tmp_path):
        src = tmp_path / "src.wav"
        src.write_bytes(b"\x00")
        mock_conv.side_effect = lambda i, o, **kw: o.write_bytes(b"\x00")

        profile = {
            "name": "NR",
            "noise_reduce": True,
            "sources": [{"file": str(src), "segments": [[0, 10]]}],
        }
        report = voice_samples.process_profile(profile, tmp_path / "out")

        assert report["segments"] == 1
        mock_nr.assert_called_once()

    def test_missing_file_reported(self, tmp_path):
        profile = {
            "name": "Missing",
            "sources": [
                {"file": "/nonexistent/path.wav", "segments": [[0, 5]]},
            ],
        }
        report = voice_samples.process_profile(profile, tmp_path / "out")
        assert report["segments"] == 0
        assert len(report["errors"]) > 0
        assert "not found" in report["errors"][0].lower() or "Source error" in report["errors"][0]

    def test_invalid_segment_reported(self, tmp_path):
        src = tmp_path / "src.wav"
        src.write_bytes(b"\x00")
        profile = {
            "name": "BadSeg",
            "sources": [{"file": str(src), "segments": ["invalid"]}],
        }
        report = voice_samples.process_profile(profile, tmp_path / "out")
        assert report["segments"] == 0
        assert len(report["errors"]) > 0


class TestMainCLI:
    def test_missing_manifest(self, tmp_path):
        ret = voice_samples.main([str(tmp_path / "no.json")])
        assert ret == 1

    def test_empty_profiles(self, tmp_path):
        m = tmp_path / "empty.json"
        m.write_text('{"profiles": []}', encoding="utf-8")
        ret = voice_samples.main([str(m)])
        assert ret == 1

    @patch("voice_samples.process_profile")
    def test_valid_manifest(self, mock_proc, tmp_path):
        mock_proc.return_value = {
            "name": "T", "voice_id": "", "segments": 1,
            "total_duration_s": 30.0, "errors": [], "files": [],
        }
        m = tmp_path / "manifest.json"
        m.write_text(json.dumps({
            "profiles": [{"name": "T", "sources": []}]
        }), encoding="utf-8")
        ret = voice_samples.main([str(m), "--output", str(tmp_path / "out")])
        assert ret == 0
        mock_proc.assert_called_once()
