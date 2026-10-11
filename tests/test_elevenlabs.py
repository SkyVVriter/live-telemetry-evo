"""Tests for the ElevenLabs TTS engine with mocked HTTP."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from live_telemetry_evo.tts.elevenlabs_engine import (
    AVAILABLE_MODELS,
    CharacterUsage,
    ElevenLabsEngine,
    VoiceProfile,
)


class TestVoiceProfile:
    def test_roundtrip(self):
        p = VoiceProfile("Test", "vid123", "eleven_flash_v2_5", 0.3, 0.8)
        d = p.to_dict()
        p2 = VoiceProfile.from_dict(d)
        assert p2.name == "Test"
        assert p2.voice_id == "vid123"
        assert p2.model_id == "eleven_flash_v2_5"
        assert p2.stability == 0.3
        assert p2.similarity_boost == 0.8

    def test_from_dict_defaults(self):
        p = VoiceProfile.from_dict({})
        assert p.name == "Default"
        assert p.stability == 0.5

    def test_from_dict_partial(self):
        p = VoiceProfile.from_dict({"name": "Custom", "stability": 0.1})
        assert p.name == "Custom"
        assert p.stability == 0.1
        assert p.similarity_boost == 0.75


class TestCharacterUsage:
    def test_add(self):
        u = CharacterUsage(budget=1000)
        u.add(100)
        assert u.chars_used == 100
        assert u.remaining == 900

    def test_month_reset(self):
        u = CharacterUsage(chars_used=500, month_key="2020-01", budget=1000)
        u.add(50)
        assert u.chars_used == 50
        assert u.month_key == u.current_month_key()

    def test_over_budget(self):
        u = CharacterUsage(budget=100)
        u.add(100)
        assert u.over_budget is True
        assert u.remaining == 0

    def test_not_over_budget(self):
        u = CharacterUsage(budget=1000)
        u.add(50)
        assert u.over_budget is False

    def test_roundtrip(self):
        u = CharacterUsage(chars_used=250, month_key="2026-10",
                           budget=5000, warned=True)
        d = u.to_dict()
        u2 = CharacterUsage.from_dict(d)
        assert u2.chars_used == 250
        assert u2.budget == 5000
        assert u2.warned is True

    def test_remaining_new_month(self):
        u = CharacterUsage(chars_used=999, month_key="1999-01", budget=1000)
        assert u.remaining == 1000


class TestElevenLabsEngine:
    def test_speak_calls_api(self, tmp_path):
        engine = ElevenLabsEngine()
        engine._api_key = "test-key-123"
        engine._profiles = [VoiceProfile("Test", "vid1", "eleven_flash_v2_5")]
        engine._active_profile_idx = 0
        engine._usage = CharacterUsage(budget=10000)

        fake_cache = tmp_path / "nocache.mp3"
        fake_audio = b"\xff\xfb\x90\x00" * 100
        with patch.object(engine, "_cache_path", return_value=fake_cache):
            with patch.object(engine, "_synthesize_api", return_value=fake_audio) as mock_synth:
                with patch.object(engine, "_play"):
                    with patch.object(engine, "_save_settings"):
                        engine.speak("Hello test")

        mock_synth.assert_called_once()
        call_args = mock_synth.call_args
        assert call_args[0][0] == "Hello test"
        assert call_args[0][1].voice_id == "vid1"
        assert call_args[0][1].model_id == "eleven_flash_v2_5"

    def test_speak_uses_cache(self, tmp_path):
        engine = ElevenLabsEngine()
        engine._api_key = "test-key"
        engine._profiles = [VoiceProfile("T", "v1", "eleven_flash_v2_5")]
        engine._usage = CharacterUsage(budget=10000)

        cache_file = tmp_path / "cached.mp3"
        fake_audio = b"\xff\xfb\x90\x00" * 50
        with patch.object(engine, "_cache_path", return_value=cache_file):
            with patch.object(engine, "_synthesize_api", return_value=fake_audio) as mock_synth:
                with patch.object(engine, "_play"):
                    with patch.object(engine, "_save_settings"):
                        engine.speak("cached phrase")
                        engine.speak("cached phrase")

        assert mock_synth.call_count == 1

    def test_speak_no_api_key_falls_back(self):
        engine = ElevenLabsEngine()
        engine._api_key = ""
        os.environ.pop("ELEVENLABS_API_KEY", None)

        with patch.object(engine, "_speak_fallback") as fb:
            engine.speak("test")
            fb.assert_called_once()

    def test_speak_over_budget_falls_back(self):
        engine = ElevenLabsEngine()
        engine._api_key = "key"
        engine._usage = CharacterUsage(budget=10)
        engine._usage.add(10)

        with patch.object(engine, "_speak_fallback") as fb:
            engine.speak("test", priority=Priority.NORMAL)
            fb.assert_called_once()

    def test_speak_over_budget_urgent_proceeds(self, tmp_path):
        engine = ElevenLabsEngine()
        engine._api_key = "key"
        engine._usage = CharacterUsage(budget=10)
        engine._usage.add(10)

        fake_cache = tmp_path / "urgent_nocache.mp3"
        fake_audio = b"\xff" * 50
        with patch.object(engine, "_cache_path", return_value=fake_cache):
            with patch.object(engine, "_synthesize_api", return_value=fake_audio) as mock_synth:
                with patch.object(engine, "_play"):
                    with patch.object(engine, "_save_settings"):
                        engine.speak("urgent", priority=Priority.URGENT)
                mock_synth.assert_called_once()

    def test_network_error_falls_back(self, tmp_path):
        import urllib.error
        engine = ElevenLabsEngine()
        engine._api_key = "key"
        engine._usage = CharacterUsage(budget=10000)

        fake_cache = tmp_path / "net_err_nocache.mp3"
        with patch.object(engine, "_cache_path", return_value=fake_cache):
            with patch.object(engine, "_synthesize_api",
                              side_effect=urllib.error.URLError("network down")):
                with patch.object(engine, "_speak_fallback") as fb:
                    engine.speak("test")
                    fb.assert_called_once()

    def test_quota_error_falls_back(self, tmp_path):
        import urllib.error
        engine = ElevenLabsEngine()
        engine._api_key = "key"
        engine._usage = CharacterUsage(budget=10000)

        fake_cache = tmp_path / "quota_err_nocache.mp3"
        with patch.object(engine, "_cache_path", return_value=fake_cache):
            with patch.object(engine, "_synthesize_api",
                              side_effect=urllib.error.HTTPError(
                                  "url", 429, "Too Many Requests", {}, None)):
                with patch.object(engine, "_speak_fallback") as fb:
                    engine.speak("test")
                    fb.assert_called_once()

    def test_usage_tracking(self):
        engine = ElevenLabsEngine()
        engine._usage = CharacterUsage(budget=10000)
        engine._usage.add(50)
        assert engine._usage.chars_used == 50

    def test_budget_warning(self):
        engine = ElevenLabsEngine()
        engine._usage = CharacterUsage(budget=100)
        engine._usage.add(80)
        engine._check_budget_warning()
        assert engine._usage.warned is True

    def test_no_early_budget_warning(self):
        engine = ElevenLabsEngine()
        engine._usage = CharacterUsage(budget=100)
        engine._usage.add(10)
        engine._check_budget_warning()
        assert engine._usage.warned is False

    def test_configure_profiles(self):
        engine = ElevenLabsEngine()
        profiles = [
            {"name": "A", "voice_id": "v1"},
            {"name": "B", "voice_id": "v2"},
        ]
        with patch.object(engine, "_save_settings"):
            engine.configure(profiles=profiles, active_profile=1)
        assert len(engine._profiles) == 2
        assert engine.active_profile.voice_id == "v2"

    def test_list_voices_from_profiles(self):
        engine = ElevenLabsEngine()
        engine._api_key = ""
        os.environ.pop("ELEVENLABS_API_KEY", None)
        engine._profiles = [
            VoiceProfile("P1", "id1"),
            VoiceProfile("P2", "id2"),
        ]
        voices = engine.list_voices()
        assert len(voices) == 2
        assert voices[0]["name"] == "P1"

    @patch("live_telemetry_evo.tts.elevenlabs_engine._fetch_voices_from_api")
    def test_list_voices_from_api(self, mock_fetch):
        mock_fetch.return_value = [
            {"id": "abc", "name": "API Voice"},
        ]
        engine = ElevenLabsEngine()
        engine._api_key = "key"
        voices = engine.list_voices()
        assert len(voices) == 1
        assert voices[0]["name"] == "API Voice"

    def test_available_models(self):
        assert "eleven_multilingual_v2" in AVAILABLE_MODELS
        assert "eleven_flash_v2_5" in AVAILABLE_MODELS
        assert "eleven_turbo_v2" in AVAILABLE_MODELS


# Need to import Priority for the budget tests
from live_telemetry_evo.tts.base import Priority
