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

from live_telemetry_evo.tts.elevenlabs_engine import DEFAULT_PROFILES


class TestGPProfile:
    def test_gp_in_defaults(self):
        names = [p.name for p in DEFAULT_PROFILES]
        assert "GP" in names

    def test_gp_voice_id(self):
        gp = next(p for p in DEFAULT_PROFILES if p.name == "GP")
        assert gp.voice_id == "L2sW7YcZbRyXB141p5PF"

    def test_gp_model(self):
        gp = next(p for p in DEFAULT_PROFILES if p.name == "GP")
        assert gp.model_id == "eleven_multilingual_v2"

    def test_gp_voice_settings(self):
        gp = next(p for p in DEFAULT_PROFILES if p.name == "GP")
        assert gp.stability == 0.8
        assert gp.similarity_boost == 0.9
        assert gp.style == 0.0
        assert gp.speed == 0.9
        assert gp.language == "en"


class TestVoiceProfileExtendedFields:
    def test_style_speed_language_roundtrip(self):
        p = VoiceProfile("Test", "vid", "model", 0.5, 0.7,
                         style=0.3, speed=0.85, language="de")
        d = p.to_dict()
        p2 = VoiceProfile.from_dict(d)
        assert p2.style == 0.3
        assert p2.speed == 0.85
        assert p2.language == "de"

    def test_defaults_for_new_fields(self):
        p = VoiceProfile.from_dict({})
        assert p.style == 0.0
        assert p.speed == 1.0
        assert p.language == ""

    def test_backward_compat_missing_fields(self):
        """Old profiles without style/speed/language load fine."""
        d = {"name": "Old", "voice_id": "vid", "stability": 0.4,
             "similarity_boost": 0.6}
        p = VoiceProfile.from_dict(d)
        assert p.style == 0.0
        assert p.speed == 1.0
        assert p.language == ""


class TestCacheKeyIncludesSettings:
    def test_different_speed_different_cache(self):
        engine = ElevenLabsEngine()
        p1 = VoiceProfile("A", "vid", "model", 0.5, 0.7,
                          style=0.0, speed=1.0)
        p2 = VoiceProfile("A", "vid", "model", 0.5, 0.7,
                          style=0.0, speed=0.9)
        c1 = engine._cache_path("hello", p1)
        c2 = engine._cache_path("hello", p2)
        assert c1 != c2

    def test_different_style_different_cache(self):
        engine = ElevenLabsEngine()
        p1 = VoiceProfile("A", "vid", "model", 0.5, 0.7, style=0.0)
        p2 = VoiceProfile("A", "vid", "model", 0.5, 0.7, style=0.5)
        assert engine._cache_path("hello", p1) != engine._cache_path("hello", p2)

    def test_different_language_different_cache(self):
        engine = ElevenLabsEngine()
        p1 = VoiceProfile("A", "vid", "model", 0.5, 0.7, language="en")
        p2 = VoiceProfile("A", "vid", "model", 0.5, 0.7, language="ru")
        assert engine._cache_path("hello", p1) != engine._cache_path("hello", p2)

    def test_same_settings_same_cache(self):
        engine = ElevenLabsEngine()
        p = VoiceProfile("A", "vid", "model", 0.5, 0.7,
                         style=0.1, speed=0.9, language="en")
        assert engine._cache_path("hello", p) == engine._cache_path("hello", p)


class TestSynthesizePayload:
    def test_payload_includes_voice_settings(self):
        """_synthesize_api must include style and speed in voice_settings."""
        engine = ElevenLabsEngine()
        engine._api_key = "test-key"
        profile = VoiceProfile("GP", "vid123", "model",
                               stability=0.8, similarity_boost=0.9,
                               style=0.0, speed=0.9, language="en")
        captured_body = {}

        def mock_api(method, path, api_key, body=None, **kw):
            captured_body["data"] = json.loads(body.decode("utf-8"))
            return b"\xff\xfb\x90\x00"  # fake MP3

        with patch("live_telemetry_evo.tts.elevenlabs_engine._api_request",
                   side_effect=mock_api):
            engine._synthesize_api("test", profile)

        payload = captured_body["data"]
        vs = payload["voice_settings"]
        assert vs["stability"] == 0.8
        assert vs["similarity_boost"] == 0.9
        assert vs["style"] == 0.0
        assert vs["speed"] == 0.9
        assert payload["language_code"] == "en"

    def test_no_language_code_when_empty(self):
        engine = ElevenLabsEngine()
        engine._api_key = "test-key"
        profile = VoiceProfile("Test", "vid", "model", language="")
        captured = {}

        def mock_api(method, path, api_key, body=None, **kw):
            captured["data"] = json.loads(body.decode("utf-8"))
            return b"\xff\xfb\x90\x00"

        with patch("live_telemetry_evo.tts.elevenlabs_engine._api_request",
                   side_effect=mock_api):
            engine._synthesize_api("test", profile)

        assert "language_code" not in captured["data"]


class TestNoAPIKeyLogged:
    def test_api_key_not_in_log_calls(self):
        """Ensure API keys are never passed to log()."""
        import live_telemetry_evo.tts.elevenlabs_engine as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            if "log(" in line or "log_action(" in line:
                assert "api_key" not in line.lower() or "never logged" in line.lower() or \
                       "no api key" in line.lower() or "# " in line, \
                    f"Potential API key leak in log call: {line.strip()}"
