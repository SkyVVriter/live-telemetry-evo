"""Tests for the PTT (Push-to-Talk) subsystem with mocked audio/input.

All tests run headless — sounddevice, pynput, and pygame are mocked.
"""
import os
import struct
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from live_telemetry_evo.ptt.recorder import (
    AudioRecorder, _pcm_to_wav, list_input_devices, find_device_by_name,
    find_output_device_by_name, resolve_output_device,
    AudioDevice,
)
from live_telemetry_evo.ptt.feedback import (
    FeedbackDedupe, idempotency_key, normalize_transcript,
    post_driver_feedback_once,
)
from live_telemetry_evo.ptt.ptt_controller import (
    PTTController, PTTMode, _generate_beep, _MIN_HOLD_DURATION_S,
)
from live_telemetry_evo.ptt.input_binding import (
    PTTBinding, BindingType, InputListener,
    list_joysticks, run_diagnostics, JoystickInfo,
    WinmmJoystick, _JOYCAPSW, _JOYINFOEX,
    _JOYERR_NOERROR, _read_buttons_winmm,
    list_joysticks_winmm,
)
from live_telemetry_evo.ptt.stt import (
    TranscriptionResult, WhisperAPIEngine, FasterWhisperEngine,
    ElevenLabsScribeEngine,
    list_stt_engines, get_stt_engine, _guess_language,
    register_stt_engine, STTEngine,
)
from live_telemetry_evo.ptt.intents import (
    IntentHandler, DriverContext, _match_intent,
)


# -- Recorder tests -------------------------------------------------------

class TestPCMToWav:
    def test_produces_valid_wav(self):
        import wave, io
        pcm = struct.pack("<10h", *range(10))
        wav = _pcm_to_wav(pcm, 16000, 1)
        assert wav[:4] == b"RIFF"
        wf = wave.open(io.BytesIO(wav), "rb")
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 10
        wf.close()

    def test_empty_pcm(self):
        wav = _pcm_to_wav(b"", 16000, 1)
        assert wav[:4] == b"RIFF"


class TestAudioRecorder:
    def test_initial_state(self):
        rec = AudioRecorder()
        assert not rec.is_recording
        assert rec.rms_level == 0.0
        assert rec.duration == 0.0

    def test_stop_without_start(self):
        rec = AudioRecorder()
        wav = rec.stop()
        assert wav[:4] == b"RIFF"

    def test_set_device(self):
        rec = AudioRecorder(device_index=3)
        assert rec._device == 3
        rec.set_device(5)
        assert rec._device == 5

    @patch("live_telemetry_evo.ptt.recorder.list_input_devices")
    def test_list_devices_empty_without_sounddevice(self, mock_list):
        mock_list.return_value = []
        assert list_input_devices() == []


# -- Beep generation -------------------------------------------------------

class TestBeepGeneration:
    def test_generates_pcm(self):
        pcm = _generate_beep(880, 0.02, 16000)
        assert len(pcm) > 0
        n_samples = 16000 * 0.02
        assert len(pcm) == int(n_samples) * 2

    def test_different_frequencies(self):
        pcm1 = _generate_beep(440, 0.01)
        pcm2 = _generate_beep(880, 0.01)
        assert pcm1 != pcm2


# -- PTT Binding -----------------------------------------------------------

class TestPTTBinding:
    def test_keyboard_binding(self):
        b = PTTBinding(type=BindingType.KEYBOARD, key_name="f5")
        assert b.display_name == "Key: f5"

    def test_joystick_binding(self):
        b = PTTBinding(type=BindingType.JOYSTICK, joy_id=0, button_id=3)
        assert b.display_name == "Joy 0 Btn 3"

    def test_round_trip(self):
        b = PTTBinding(type=BindingType.KEYBOARD, key_name="space")
        d = b.to_dict()
        b2 = PTTBinding.from_dict(d)
        assert b2.type == b.type
        assert b2.key_name == b.key_name

    def test_joystick_round_trip(self):
        b = PTTBinding(type=BindingType.JOYSTICK, joy_id=1, button_id=7)
        d = b.to_dict()
        b2 = PTTBinding.from_dict(d)
        assert b2.joy_id == 1
        assert b2.button_id == 7


# -- PTT Controller --------------------------------------------------------

class TestPTTController:
    def test_initial_state(self):
        ctrl = PTTController()
        assert not ctrl.is_active
        assert ctrl.binding is None
        assert ctrl.mode == PTTMode.HOLD

    def test_set_mode(self):
        ctrl = PTTController()
        ctrl.set_mode(PTTMode.TOGGLE)
        assert ctrl.mode == PTTMode.TOGGLE

    def test_set_binding(self):
        ctrl = PTTController()
        b = PTTBinding(type=BindingType.KEYBOARD, key_name="f5")
        ctrl.set_binding(b)
        assert ctrl.binding is not None
        assert ctrl.binding.key_name == "f5"

    def test_hold_mode_short_press_ignored(self):
        captured = []
        ctrl = PTTController(
            recorder=MagicMock(
                start=MagicMock(return_value=True),
                stop=MagicMock(return_value=b"RIFF"),
                is_recording=True,
            ),
            on_audio_captured=lambda wav, dur: captured.append((wav, dur)),
            mode=PTTMode.HOLD,
        )
        ctrl._on_press()
        assert ctrl.is_active
        ctrl._press_time = time.monotonic()
        ctrl._on_release()
        assert len(captured) == 0

    def test_hold_mode_long_press_captured(self):
        captured = []
        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_recorder.stop.return_value = b"RIFF" + b"\x00" * 100
        ctrl = PTTController(
            recorder=mock_recorder,
            on_audio_captured=lambda wav, dur: captured.append((wav, dur)),
            mode=PTTMode.HOLD,
        )
        ctrl._on_press()
        ctrl._press_time = time.monotonic() - 1.0
        ctrl._on_release()
        assert len(captured) == 1

    def test_toggle_mode(self):
        captured = []
        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_recorder.stop.return_value = b"RIFF" + b"\x00" * 100
        ctrl = PTTController(
            recorder=mock_recorder,
            on_audio_captured=lambda wav, dur: captured.append((wav, dur)),
            mode=PTTMode.TOGGLE,
        )
        ctrl._on_press()
        assert ctrl.is_active
        ctrl._press_time = time.monotonic() - 1.0
        ctrl._on_press()
        assert not ctrl.is_active
        assert len(captured) == 1

    def test_mute_tts_callback(self):
        mute_calls = []
        ctrl = PTTController(
            recorder=MagicMock(start=MagicMock(return_value=True),
                               stop=MagicMock(return_value=b"RIFF")),
            on_mute_tts=lambda m: mute_calls.append(m),
            mode=PTTMode.HOLD,
        )
        ctrl._on_press()
        assert mute_calls == [True]
        ctrl._press_time = time.monotonic() - 1.0
        ctrl._on_release()
        assert mute_calls == [True, False]

    def test_max_duration_timeout(self):
        captured = []
        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_recorder.stop.return_value = b"RIFF" + b"\x00" * 100
        ctrl = PTTController(
            recorder=mock_recorder,
            on_audio_captured=lambda wav, dur: captured.append((wav, dur)),
            mode=PTTMode.HOLD,
            max_duration_s=0.2,
        )
        ctrl._on_press()
        time.sleep(0.4)
        assert not ctrl.is_active
        assert len(captured) == 1


# -- STT engines -----------------------------------------------------------

class TestSTTRegistry:
    def test_list_engines(self):
        engines = list_stt_engines()
        assert "whisper-api" in engines
        assert "faster-whisper" in engines
        assert "elevenlabs-scribe" in engines

    def test_get_engine(self):
        e = get_stt_engine("whisper-api")
        assert e is not None
        assert e.name == "whisper-api"

    def test_get_unknown_engine(self):
        assert get_stt_engine("nonexistent") is None


class TestGuessLanguage:
    def test_russian(self):
        assert _guess_language("Привет мир") == "ru"

    def test_english(self):
        assert _guess_language("Hello world") == "en"

    def test_mixed_defaults_russian(self):
        assert _guess_language("Hello Привет") == "ru"


class TestWhisperAPI:
    def test_no_api_key(self):
        engine = WhisperAPIEngine()
        engine.api_key = ""
        with patch.dict("os.environ", {}, clear=True):
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)
        assert result.text == ""


class TestFasterWhisper:
    def test_no_package(self):
        engine = FasterWhisperEngine()
        engine._model = None
        with patch.dict("sys.modules", {"faster_whisper": None}):
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)
        assert result.text == ""


class TestElevenLabsScribe:
    def test_in_registry(self):
        e = get_stt_engine("elevenlabs-scribe")
        assert e is not None
        assert e.name == "elevenlabs-scribe"

    def test_no_api_key_returns_empty(self):
        engine = ElevenLabsScribeEngine()
        engine.api_key = ""
        with patch.dict("os.environ", {}, clear=True), \
             patch("live_telemetry_evo.ptt.stt.ElevenLabsScribeEngine._resolve_api_key",
                   return_value=""):
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)
        assert result.text == ""

    def test_successful_transcription(self):
        engine = ElevenLabsScribeEngine()
        engine.api_key = "test-key"

        response_json = json.dumps({"text": "Привет мир"}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_json
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)

        assert result.text == "Привет мир"
        assert result.language == "ru"
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert req.get_header("Xi-api-key") == "test-key"
        assert "multipart/form-data" in req.get_header("Content-type")
        body = req.data
        assert b"scribe_v1" in body
        assert b"recording.wav" in body

    def test_language_code_sent(self):
        engine = ElevenLabsScribeEngine()
        engine.api_key = "test-key"
        engine.language_code = "en"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"text": "hello"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = engine.transcribe(b"RIFF" + b"\x00" * 100, language="en")

        assert result.text == "hello"
        body = mock_open.call_args[0][0].data
        assert b"language_code" in body
        assert b"en" in body

    def test_http_error_logged(self):
        import urllib.error
        engine = ElevenLabsScribeEngine()
        engine.api_key = "test-key"

        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(
                       "url", 429, "rate limit", {}, None)):
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)

        assert result.text == ""

    def test_timeout_logged(self):
        engine = ElevenLabsScribeEngine()
        engine.api_key = "test-key"

        with patch("urllib.request.urlopen",
                   side_effect=TimeoutError("timed out")):
            result = engine.transcribe(b"RIFF" + b"\x00" * 100)

        assert result.text == ""

    def test_resolves_tts_api_key(self, tmp_path):
        """Falls back to the shared ElevenLabs TTS key."""
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            from live_telemetry_evo.settings import _write
            _write({"elevenlabs": {"api_key": "shared-tts-key"}})
            engine = ElevenLabsScribeEngine()
            engine.api_key = ""
            with patch.dict("os.environ", {}, clear=True):
                key = engine._resolve_api_key()
            assert key == "shared-tts-key"
        finally:
            settings._positions_path = orig

    def test_no_key_leak_in_source(self):
        """Ensure API keys are never logged in stt.py."""
        import live_telemetry_evo.ptt.stt as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for i, line in enumerate(source.splitlines(), 1):
            if "log(" in line:
                assert "api_key" not in line.lower() or \
                       "no api key" in line.lower(), \
                    f"Potential key leak at line {i}: {line.strip()}"

    def test_default_stt_engine_is_scribe(self, tmp_path):
        """New installs should default to elevenlabs-scribe."""
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            cfg = settings.load_ptt_settings()
            assert cfg["stt_engine"] == "elevenlabs-scribe"
        finally:
            settings._positions_path = orig


import json


# -- Intent handling -------------------------------------------------------

class TestIntentMatching:
    def test_status_ru(self):
        assert _match_intent("статус") == "status"

    def test_status_en(self):
        assert _match_intent("status") == "status"

    def test_gaps(self):
        assert _match_intent("гапы") == "gaps"

    def test_fuel(self):
        assert _match_intent("топливо") == "fuel"

    def test_incidents(self):
        assert _match_intent("инки") == "incidents"

    def test_unknown(self):
        assert _match_intent("что-то другое") is None

    def test_empty(self):
        assert _match_intent("") is None

    def test_with_punctuation(self):
        assert _match_intent("статус!") == "status"

    def test_multi_word(self):
        assert _match_intent("скажи мне статус") == "status"


class TestIntentHandler:
    def test_local_command_speaks(self):
        spoken = []
        handler = IntentHandler(
            on_speak=lambda t: spoken.append(t),
            get_context=lambda: DriverContext(position=3, lap=5, incidents=2),
        )
        result = handler.handle("статус")
        assert result == "status"
        assert len(spoken) == 1
        assert "3" in spoken[0]

    def test_feedback_dispatched(self):
        feedback = []
        handler = IntentHandler(
            on_driver_feedback=lambda t, d, c: feedback.append((t, d)),
            get_context=lambda: DriverContext(),
        )
        result = handler.handle("тут какая-то проблема с балансом")
        assert result == "feedback"
        assert len(feedback) == 1

    def test_empty_text(self):
        handler = IntentHandler()
        assert handler.handle("") == "empty"

    def test_gaps_response(self):
        spoken = []
        handler = IntentHandler(
            on_speak=lambda t: spoken.append(t),
            get_context=lambda: DriverContext(
                gap_ahead=1.5, car_ahead_name="Hamilton",
                gap_behind=3.2, car_behind_name="Verstappen",
            ),
        )
        handler.handle("гапы")
        assert "Hamilton" in spoken[0]
        assert "1.5" in spoken[0]

    def test_fuel_response(self):
        spoken = []
        handler = IntentHandler(
            on_speak=lambda t: spoken.append(t),
            get_context=lambda: DriverContext(
                fuel_remaining=35.0, fuel_per_lap=2.1,
            ),
        )
        handler.handle("топливо")
        assert "35.0" in spoken[0]
        assert "2.1" in spoken[0]

    def test_incidents_response(self):
        spoken = []
        handler = IntentHandler(
            on_speak=lambda t: spoken.append(t),
            get_context=lambda: DriverContext(
                incidents=5, incident_limit_dt=17, incident_limit_dq=25,
            ),
        )
        handler.handle("инки")
        assert "5" in spoken[0]
        assert "12" in spoken[0]  # 17-5 remaining


# -- PTT Settings round-trip -----------------------------------------------

class TestPTTSettings:
    def test_save_and_load(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "s.json"
        try:
            from live_telemetry_evo.settings import load_ptt_settings, save_ptt_settings
            save_ptt_settings(
                enabled=True,
                mode="toggle",
                max_duration_s=10.0,
                device_index=2,
                stt_engine="faster-whisper",
                binding={"type": "keyboard", "key_name": "f5"},
            )
            cfg = load_ptt_settings()
            assert cfg["enabled"] is True
            assert cfg["mode"] == "toggle"
            assert cfg["max_duration_s"] == 10.0
            assert cfg["device_index"] == 2
            assert cfg["stt_engine"] == "faster-whisper"
            assert cfg["binding"]["key_name"] == "f5"
            assert cfg["wheel_backend"] == "winmm"
        finally:
            settings._positions_path = orig


# -- TTS scheduler muting --------------------------------------------------

class TestTTSMuting:
    def test_muted_property(self):
        from live_telemetry_evo.tts.base import Priority, VoiceEngine
        from live_telemetry_evo.tts.scheduler import SpeechScheduler

        class DummyEngine(VoiceEngine):
            name = "dummy"
            def speak(self, text, priority=Priority.NORMAL):
                pass
            def list_voices(self):
                return []
            def settings_schema(self):
                return {}

        sched = SpeechScheduler(DummyEngine())
        assert not sched.muted
        sched.muted = True
        assert sched.muted
        sched.muted = False
        assert not sched.muted

    def test_muted_blocks_speech(self):
        from live_telemetry_evo.tts.base import Priority, VoiceEngine
        from live_telemetry_evo.tts.scheduler import SpeechScheduler

        class TrackingEngine(VoiceEngine):
            name = "tracking"
            def __init__(self):
                self.spoken = []
            def speak(self, text, priority=Priority.NORMAL):
                self.spoken.append(text)
                time.sleep(0.01)
            def list_voices(self):
                return []
            def settings_schema(self):
                return {}

        eng = TrackingEngine()
        sched = SpeechScheduler(eng)
        sched.muted = True
        sched.start()
        sched.enqueue("should not speak")
        time.sleep(0.3)
        assert len(eng.spoken) == 0
        sched.muted = False
        time.sleep(0.5)
        sched.stop()
        assert len(eng.spoken) == 1


# -- race-worker driver_feedback event type ---------------------------------

class TestDriverFeedbackEventType:
    def test_enum_value(self):
        import sys
        rw_path = str(Path(__file__).parent.parent / "iracing" / "race-worker")
        sys.path.insert(0, rw_path)
        try:
            from app.models import EventType
            assert EventType.driver_feedback.value == "driver_feedback"
        except ImportError:
            pytest.skip("race-worker dependencies not installed")
        finally:
            sys.path.remove(rw_path)


# -- Device listing with hostapi dedup + name persistence -------------------

class TestDeviceListDedup:
    def test_dedup_prefers_wasapi(self):
        """Devices with the same name but different hostapis should be
        deduped, preferring WASAPI."""
        wasapi = AudioDevice(
            index=0, name="Microphone", max_input_channels=2,
            default_samplerate=44100, hostapi_name="Windows WASAPI")
        mme = AudioDevice(
            index=1, name="Microphone", max_input_channels=2,
            default_samplerate=44100, hostapi_name="MME")
        from live_telemetry_evo.ptt import recorder
        orig = recorder.list_input_devices
        recorder.list_input_devices = lambda: [wasapi, mme]
        try:
            idx = find_device_by_name("Microphone")
            assert idx == 0
        finally:
            recorder.list_input_devices = orig

    def test_find_device_by_name_not_found(self):
        from live_telemetry_evo.ptt import recorder
        orig = recorder.list_input_devices
        recorder.list_input_devices = lambda: []
        try:
            assert find_device_by_name("NonExistent") is None
        finally:
            recorder.list_input_devices = orig

    def test_audio_device_hostapi_field(self):
        d = AudioDevice(
            index=5, name="USB Mic", max_input_channels=1,
            default_samplerate=48000, hostapi_name="Windows WASAPI")
        assert d.hostapi_name == "Windows WASAPI"
        assert d.index == 5


class TestPTTSettingsDevicePersistence:
    def test_device_name_round_trip(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            settings.save_ptt_settings(
                device_name="USB Microphone",
                device_hostapi="Windows WASAPI",
                device_index=3,
            )
            cfg = settings.load_ptt_settings()
            assert cfg["device_name"] == "USB Microphone"
            assert cfg["device_hostapi"] == "Windows WASAPI"
            assert cfg["device_index"] == 3
        finally:
            settings._positions_path = orig

    def test_default_device_name_empty(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            cfg = settings.load_ptt_settings()
            assert cfg["device_name"] == ""
            assert cfg["device_hostapi"] == ""
            assert cfg["wheel_backend"] == "winmm"
            assert cfg["output_device_name"] == ""
            assert cfg["output_device_hostapi"] == ""
            assert cfg["mic_keep_warm"] is False
            assert cfg["mic_grace_s"] == 0.45
        finally:
            settings._positions_path = orig

    def test_output_device_round_trip(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "positions.json"
        try:
            settings.save_ptt_settings(
                output_device_name="Headphones (Px7 S3 Stereo)",
                output_device_hostapi="Windows WASAPI",
                output_device_index=5,
                mic_grace_s=0.5,
                mic_keep_warm=True,
            )
            cfg = settings.load_ptt_settings()
            assert cfg["output_device_name"] == "Headphones (Px7 S3 Stereo)"
            assert cfg["output_device_hostapi"] == "Windows WASAPI"
            assert cfg["output_device_index"] == 5
            assert cfg["mic_grace_s"] == 0.5
            assert cfg["mic_keep_warm"] is True
        finally:
            settings._positions_path = orig


# -- PTT binding signal bridge pattern -------------------------------------

class TestBindingSignalBridge:
    def test_binding_round_trip(self):
        """PTTBinding -> to_dict -> from_dict round-trip."""
        b = PTTBinding(type=BindingType.KEYBOARD, key_name="f5")
        d = b.to_dict()
        b2 = PTTBinding.from_dict(d)
        assert b2.type == BindingType.KEYBOARD
        assert b2.key_name == "f5"
        assert b2.display_name == "Key: f5"

    def test_joystick_binding_display(self):
        b = PTTBinding(type=BindingType.JOYSTICK, joy_id=0, button_id=7)
        assert b.display_name == "Joy 0 Btn 7"
        d = b.to_dict()
        b2 = PTTBinding.from_dict(d)
        assert b2.type == BindingType.JOYSTICK
        assert b2.joy_id == 0
        assert b2.button_id == 7


# -- App quitOnLastWindowClosed check ----------------------------------------

class TestDriverFeedbackAlwaysSent:
    """Every PTT transcript must be sent as driver_feedback, including
    local commands (status, fuel, etc.)."""

    def test_local_command_also_sends_feedback(self):
        spoken = []
        feedback = []

        handler = IntentHandler(
            on_speak=lambda t: spoken.append(t),
            on_driver_feedback=lambda t, d, c: feedback.append(
                {"text": t, "duration": d}),
            get_context=lambda: DriverContext(position=3, lap=5),
        )
        result = handler.handle("статус", audio_duration=1.5)
        assert result == "status"
        assert len(spoken) == 1
        assert len(feedback) == 1
        assert feedback[0]["text"] == "статус"
        assert feedback[0]["duration"] == 1.5

    def test_unmatched_sends_feedback(self):
        feedback = []
        handler = IntentHandler(
            on_driver_feedback=lambda t, d, c: feedback.append(t),
            get_context=lambda: DriverContext(),
        )
        handler.handle("запасной выход слева", audio_duration=2.0)
        assert len(feedback) == 1
        assert feedback[0] == "запасной выход слева"

    def test_context_has_telemetry_fields(self):
        ctx = DriverContext(
            speed_kmh=245.3, throttle=0.95, brake=0.0,
            steering_rad=-0.12, yaw_rate=0.03,
        )
        assert ctx.speed_kmh == 245.3
        assert ctx.throttle == 0.95
        assert ctx.steering_rad == -0.12

    def test_feedback_payload_structure(self):
        """The on_driver_feedback callback receives DriverContext with
        telemetry fields that the app.py handler serializes into the
        event payload."""
        captured = []

        def on_fb(text, duration, ctx):
            captured.append({
                "text": text,
                "speed": ctx.speed_kmh,
                "throttle": ctx.throttle,
                "brake": ctx.brake,
                "steering": ctx.steering_rad,
                "yaw": ctx.yaw_rate,
            })

        handler = IntentHandler(
            on_driver_feedback=on_fb,
            get_context=lambda: DriverContext(
                speed_kmh=200.0, throttle=0.8, brake=0.1,
                steering_rad=-0.05, yaw_rate=0.02,
            ),
        )
        handler.handle("radio check", audio_duration=1.0)
        assert len(captured) == 1
        assert captured[0]["speed"] == 200.0
        assert captured[0]["throttle"] == 0.8
        assert captured[0]["brake"] == 0.1

    def test_empty_text_not_sent(self):
        feedback = []
        handler = IntentHandler(
            on_driver_feedback=lambda t, d, c: feedback.append(t),
            get_context=lambda: DriverContext(),
        )
        handler.handle("", audio_duration=0.0)
        assert len(feedback) == 0


class TestWinmmBackend:
    """Test the winmm joystick backend with mocked ctypes."""

    def test_joycapsw_struct_fields(self):
        caps = _JOYCAPSW()
        assert hasattr(caps, "szPname")
        assert hasattr(caps, "wNumButtons")

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="JOYCAPSW Windows layout (WCHAR=2, UINT=4)")
    def test_joycapsw_size_is_728(self):
        # MMSYSTEM JOYCAPSW: szOEMVxD is MAX_JOYSTICKOEMVXDNAME (260 WCHARs)
        assert ctypes.sizeof(_JOYCAPSW) == 728

    def test_joyinfoex_struct_fields(self):
        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(_JOYINFOEX)
        assert hasattr(info, "dwButtons")
        assert info.dwSize > 0

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="JOYINFOEX Windows layout (DWORD=4)")
    def test_joyinfoex_size_is_52(self):
        assert ctypes.sizeof(_JOYINFOEX) == 52

    def test_list_joysticks_winmm_with_mock(self):
        from live_telemetry_evo.ptt import input_binding as mod
        mock_caps = _JOYCAPSW()
        mock_caps.szPname = "Moza R16"
        mock_caps.wNumButtons = 12

        def mock_num():
            return 2

        def mock_caps_fn(jid, ptr, sz):
            if jid == 0:
                ctypes.memmove(ptr, ctypes.byref(mock_caps),
                               min(sz, ctypes.sizeof(_JOYCAPSW)))
                return _JOYERR_NOERROR
            return 167

        orig = mod._winmm_funcs
        mod._winmm_funcs = (mock_num, mock_caps_fn, None)
        try:
            joys = list_joysticks_winmm()
            assert len(joys) == 1
            assert joys[0].name == "Moza R16"
            assert joys[0].num_buttons == 12
        finally:
            mod._winmm_funcs = orig

    def test_read_buttons_winmm_with_mock(self):
        from live_telemetry_evo.ptt import input_binding as mod

        def mock_joyGetPosEx(jid, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(_JOYINFOEX)).contents
            info.dwButtons = 0b1010
            return _JOYERR_NOERROR

        orig = mod._winmm_funcs
        mod._winmm_funcs = (None, None, mock_joyGetPosEx)
        try:
            bitmask = _read_buttons_winmm(0)
            assert bitmask & (1 << 1)
            assert bitmask & (1 << 3)
            assert not (bitmask & (1 << 0))
        finally:
            mod._winmm_funcs = orig

    def test_read_buttons_returns_zero_on_error(self):
        from live_telemetry_evo.ptt import input_binding as mod
        orig = mod._winmm_funcs
        mod._winmm_funcs = False
        try:
            assert _read_buttons_winmm(0) == 0
        finally:
            mod._winmm_funcs = orig


class TestDIJoystick2DataFormat:
    """Build DIDATAFORMAT/DIOBJECTDATAFORMAT for DIJOYSTATE2 (any OS)."""

    def test_dijoystate2_size_and_offsets(self):
        import ctypes
        from live_telemetry_evo.ptt.dinput_format import (
            DIJOYSTATE2_SIZE, _DI_BUTTON_COUNT, _DIJOYSTATE2,
        )
        assert ctypes.sizeof(_DIJOYSTATE2) == DIJOYSTATE2_SIZE == 272
        assert _DIJOYSTATE2.lX.offset == 0
        assert _DIJOYSTATE2.rgbButtons.offset == 48
        assert _DIJOYSTATE2.lVX.offset == 176
        assert _DI_BUTTON_COUNT == 128
        state = _DIJOYSTATE2()
        assert len(state.rgbButtons) == 128

    def test_builds_c_dfDIJoystick2(self):
        import ctypes
        from live_telemetry_evo.ptt.dinput_format import (
            DIDATAFORMAT_SIZE, DIDF_ABSAXIS, DIOBJECTDATAFORMAT_SIZE,
            DIJOYSTATE2_SIZE, _DF_JOYSTICK2_NUM_OBJS,
            _DIDATAFORMAT, _DIOBJECTDATAFORMAT, _DIJOYSTATE2,
            build_dijoystate2_data_format, c_dfDIJoystick2,
            sdk_df_joystick2_objects, validate_data_format,
        )
        built = build_dijoystate2_data_format()
        assert ctypes.sizeof(_DIDATAFORMAT) == DIDATAFORMAT_SIZE == 32
        assert ctypes.sizeof(_DIOBJECTDATAFORMAT) == DIOBJECTDATAFORMAT_SIZE == 24
        assert _DIDATAFORMAT.rgodf.offset == 24
        assert _DIOBJECTDATAFORMAT.pguid.offset == 0
        assert _DIOBJECTDATAFORMAT.dwOfs.offset == 8
        assert built.num_objs == _DF_JOYSTICK2_NUM_OBJS == 164
        assert built.data_size == DIJOYSTATE2_SIZE == 272
        assert built.obj_size == 24
        assert built.fmt_size == 32
        assert built.fmt.dwFlags == DIDF_ABSAXIS
        assert built.fmt.dwNumObjs == 164
        assert built.fmt.dwDataSize == 272
        assert built.fmt.rgodf
        btn0 = _DIJOYSTATE2.rgbButtons.offset
        assert built.objects[12].dwOfs == btn0
        assert built.objects[12 + 127].dwOfs == btn0 + 127
        assert c_dfDIJoystick2() is c_dfDIJoystick2()
        assert validate_data_format(built) == []

    def test_matches_sdk_object_table(self):
        """Byte-for-byte match of (dwOfs, dwType, dwFlags) vs SDK/Wine table."""
        from live_telemetry_evo.ptt.dinput_format import (
            DIDFT_ANYINSTANCE, DIDFT_OPTIONAL,
            build_dijoystate2_data_format, sdk_df_joystick2_objects,
        )
        built = build_dijoystate2_data_format()
        expected = sdk_df_joystick2_objects()
        assert len(built.objects) == len(expected) == 164
        got = [(int(o.dwOfs), int(o.dwType), int(o.dwFlags))
               for o in built.objects]
        assert got == expected
        for o in built.objects:
            assert o.dwOfs < built.data_size
            assert o.dwType & DIDFT_OPTIONAL
            assert o.dwType & DIDFT_ANYINSTANCE

    def test_dinput_module_does_not_lookup_dll_symbol(self):
        src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "ptt" / "dinput.py"
        text = src.read_text(encoding="utf-8")
        assert "in_dll" not in text
        assert "c_dfDIJoystick2()" in text or "c_dfDIJoystick2" in text

    def test_device_vtable_slots(self):
        """SetDataFormat is 11 (not GetDeviceData=10); Poll is 25 (not Unacquire=8)."""
        src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "ptt" / "dinput.py"
        text = src.read_text(encoding="utf-8")
        assert "VT_DEV_SETDATAFORMAT = 11" in text
        assert "VT_DEV_GETDEVICEDATA = 10" in text
        assert "VT_DEV_POLL = 25" in text
        assert "VT_DEV_UNACQUIRE = 8" in text
        assert "dev_vt[VT_DEV_SETDATAFORMAT]" in text
        assert "dev_vt[VT_DEV_POLL]" in text
        assert "dev_vt[10]" not in text


class TestPygamePTTBackend:
    """PTT listing/capture must work when DirectInput cannot poll."""

    def test_list_joysticks_default_is_winmm_no_pygame_fallback(self):
        from live_telemetry_evo.ptt import input_binding as mod
        winmm = [{"index": 0, "name": "Generic USB Joystick", "buttons": 32,
                  "backend": "winmm"}]
        pg = [{"index": 0, "name": "MOZA R16 Ultra Base", "buttons": 128,
               "backend": "pygame"}]
        with patch.object(mod, "_list_dinput", return_value=[]), \
             patch.object(mod, "_list_pygame", return_value=pg) as pg_list, \
             patch.object(mod, "_list_winmm", return_value=winmm), \
             patch.object(mod, "resolve_wheel_backend", return_value="winmm"):
            result = list_joysticks()
        assert result[0]["backend"] == "winmm"
        assert result[0]["buttons"] == 32
        pg_list.assert_not_called()

    def test_list_joysticks_backend_off_is_empty(self):
        from live_telemetry_evo.ptt import input_binding as mod
        with patch.object(mod, "_list_pygame") as pg_list, \
             patch.object(mod, "_list_dinput") as di_list, \
             patch.object(mod, "_list_winmm") as wm_list:
            result = list_joysticks(backend="off")
        assert result == []
        pg_list.assert_not_called()
        di_list.assert_not_called()
        wm_list.assert_not_called()

    def test_list_joysticks_explicit_pygame(self):
        from live_telemetry_evo.ptt import input_binding as mod
        pg = [{"index": 0, "name": "MOZA R16 Ultra Base", "buttons": 128,
               "backend": "pygame"}]
        with patch.object(mod, "_list_pygame", return_value=pg), \
             patch.object(mod, "_list_winmm", return_value=[]):
            result = list_joysticks(backend="pygame")
        assert result[0]["backend"] == "pygame"
        assert result[0]["buttons"] == 128

    def test_pygame_assign_captures_high_button(self):
        from live_telemetry_evo.ptt.pygame_joy import FakeJoyBackend
        backend = FakeJoyBackend()
        captured: list = []
        listener = InputListener(joy_backend=backend)
        listener.start_assign(captured.append)
        time.sleep(0.03)
        backend.emit(0, 64, True)
        deadline = time.monotonic() + 1.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        listener.stop()
        assert captured, "pygame backend did not capture a button press"
        assert captured[0].type == BindingType.JOYSTICK
        assert captured[0].joy_id == 0
        assert captured[0].button_id == 64

    def test_pygame_logs_press_release_and_assign(self):
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.pygame_joy import FakeJoyBackend
        backend = FakeJoyBackend()
        logs: list[str] = []
        with patch.object(mod, "log", side_effect=lambda m: logs.append(m)):
            listener = InputListener(joy_backend=backend)
            listener.start_assign(lambda _b: None)
            time.sleep(0.03)
            backend.emit(0, 3, True)
            time.sleep(0.05)
            listener.stop()
        joined = "\n".join(logs)
        assert "event device=0 name='MOZA R16 Ultra Base' button=3 pressed" in joined
        assert "assign joy=0 btn=3" in joined

    def test_pygame_press_release_honors_binding(self):
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.pygame_joy import FakeJoyBackend
        backend = FakeJoyBackend()
        events: list[str] = []
        logs: list[str] = []
        with patch.object(mod, "log", side_effect=lambda m: logs.append(m)):
            listener = InputListener(joy_backend=backend)
            listener.set_binding(PTTBinding(
                type=BindingType.JOYSTICK, joy_id=0, button_id=7))
            listener.start(lambda: events.append("press"),
                           lambda: events.append("release"))
            time.sleep(0.03)
            backend.emit(0, 7, True, name="R16")
            backend.emit(0, 7, False, name="R16")
            time.sleep(0.05)
            listener.stop()
        assert events == ["press", "release"]
        joined = "\n".join(logs)
        assert "button=7 pressed" in joined
        assert "button=7 released" in joined
        assert "[ptt] down btn=7" in joined
        assert "[ptt] up" in joined

    def test_assign_press_from_worker_does_not_block_gui(self):
        """Worker-thread press must not stall the caller (GUI) thread.

        A 2s join / pygame-on-GUI-thread freeze would fail this.
        """
        from live_telemetry_evo.ptt.pygame_joy import FakeJoyBackend
        backend = FakeJoyBackend()
        captured: list = []
        delivered = threading.Event()

        def on_capture(binding):
            captured.append((threading.current_thread().name, binding))
            delivered.set()

        listener = InputListener(joy_backend=backend)
        listener.start_assign(on_capture, timeout_s=5)

        def _emit():
            time.sleep(0.03)
            backend.emit(0, 5, True)

        worker = threading.Thread(target=_emit, name="fake-joy-worker")
        worker.start()

        t0 = time.monotonic()
        gui_ticks = 0
        while time.monotonic() - t0 < 0.8:
            gui_ticks += 1
            if delivered.is_set() and gui_ticks >= 8:
                break
            time.sleep(0.01)
        worker.join(0.5)
        listener.stop()
        elapsed = time.monotonic() - t0

        assert captured, "assign callback never ran"
        assert captured[0][1].button_id == 5
        assert gui_ticks >= 8, f"GUI only ticked {gui_ticks} times"
        assert elapsed < 0.9, f"GUI stalled for {elapsed:.2f}s"

    def test_stop_from_joy_thread_does_not_deadlock(self):
        listener = InputListener()
        listener._joy_thread = threading.current_thread()
        t0 = time.monotonic()
        listener.stop()
        assert time.monotonic() - t0 < 0.5
        assert listener._joy_thread is None

    def test_no_pygame_display_set_mode(self):
        src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "ptt"
        for name in ("pygame_joy.py", "input_binding.py"):
            text = (src / name).read_text(encoding="utf-8")
            assert "display.set_mode(" not in text
            assert "display.init(" not in text


class TestGuiWatchdog:
    def test_dump_traceback_later_is_used(self):
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "gui_watchdog.py")
        text = src.read_text(encoding="utf-8")
        assert "dump_traceback_later" in text
        assert "STALL_S = 3.0" in text

    def test_app_starts_watchdog(self):
        src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "app.py"
        text = src.read_text(encoding="utf-8")
        assert "start_gui_watchdog" in text


class TestDInputBackend:
    """Test the DirectInput8 backend structures and mocked listing."""

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="DirectInput8 is Windows-only")
    def test_dinput_device_dataclass(self):
        from live_telemetry_evo.ptt.dinput import DInputDevice
        d = DInputDevice(
            instance_name="Moza R16",
            product_name="Moza R16 Base",
            guid_instance=b"\x00" * 16,
            guid_product=b"\x34\x12\x78\x56" + b"\x00" * 12,
            dev_type=0x0215,
            num_buttons=128,
            vid_pid="1234:5678",
        )
        assert d.display_name == "Moza R16 Base"
        assert d.vid_pid == "1234:5678"
        assert d.num_buttons == 128

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="DirectInput8 is Windows-only")
    def test_dijoystate2_has_128_buttons(self):
        from live_telemetry_evo.ptt.dinput import _DIJOYSTATE2, _DI_BUTTON_COUNT
        state = _DIJOYSTATE2()
        assert _DI_BUTTON_COUNT == 128
        assert len(state.rgbButtons) == 128

    def test_binding_supports_high_button_ids(self):
        b = PTTBinding(type=BindingType.JOYSTICK, joy_id=0, button_id=64)
        assert b.display_name == "Joy 0 Btn 64"
        d = b.to_dict()
        b2 = PTTBinding.from_dict(d)
        assert b2.button_id == 64

    def test_joystick_info_dataclass(self):
        j = JoystickInfo(index=0, name="Wheel", buttons=128,
                         backend="dinput8", vid_pid="2345:6789")
        assert j.backend == "dinput8"
        assert j.vid_pid == "2345:6789"


class TestDiagnostics:
    def test_run_diagnostics_returns_string(self):
        report = run_diagnostics(active_backend="winmm")
        assert isinstance(report, str)
        assert "Joystick Diagnostics" in report
        assert "DirectInput8" in report
        assert "winmm" in report
        assert "pygame" in report
        assert "FFB policy" in report
        assert "not started" in report
        assert "HIDAPI=0" in report

    def test_list_joysticks_returns_backend_field(self):
        """list_joysticks() entries include a 'backend' field."""
        from live_telemetry_evo.ptt import input_binding as mod
        mock_result = [{"index": 0, "name": "MockWheel", "buttons": 16,
                        "backend": "dinput8", "vid_pid": "1234:5678"}]
        with patch.object(mod, '_list_dinput', return_value=mock_result):
            result = list_joysticks(backend="dinput")
            assert len(result) == 1
            assert result[0]["backend"] == "dinput8"
            assert result[0]["name"] == "MockWheel"


class TestFFBSafePolling:
    """Wheel polling must never touch force feedback."""

    def test_default_backend_is_winmm(self):
        from live_telemetry_evo.ptt.ffb_safe import (
            DEFAULT_WHEEL_BACKEND, normalize_wheel_backend,
        )
        assert DEFAULT_WHEEL_BACKEND == "winmm"
        assert normalize_wheel_backend(None) == "winmm"
        assert normalize_wheel_backend("DirectInput") == "dinput"
        assert normalize_wheel_backend("dinput8") == "dinput"
        assert normalize_wheel_backend("bogus") == "winmm"

    def test_cooperative_level_never_exclusive(self):
        from live_telemetry_evo.ptt.ffb_safe import (
            DISCL_BACKGROUND, DISCL_EXCLUSIVE, DISCL_FOREGROUND,
            DISCL_NONEXCLUSIVE, DISCL_SAFE, cooperative_level_flags,
        )
        flags = cooperative_level_flags()
        assert flags == DISCL_SAFE
        assert flags == (DISCL_NONEXCLUSIVE | DISCL_BACKGROUND)
        assert not (flags & DISCL_EXCLUSIVE)
        assert not (flags & DISCL_FOREGROUND)

    def test_sdl_hints_disable_hidapi_and_rawinput(self):
        from live_telemetry_evo.ptt.ffb_safe import (
            SDL_FFB_SAFE_HINTS, apply_sdl_ffb_safe_hints,
        )
        apply_sdl_ffb_safe_hints()
        assert SDL_FFB_SAFE_HINTS["SDL_HINT_JOYSTICK_HIDAPI"] == "0"
        assert SDL_FFB_SAFE_HINTS["SDL_JOYSTICK_HIDAPI"] == "0"
        assert SDL_FFB_SAFE_HINTS["SDL_HINT_JOYSTICK_RAWINPUT"] == "0"
        assert os.environ["SDL_HINT_JOYSTICK_HIDAPI"] == "0"
        assert os.environ["SDL_JOYSTICK_HIDAPI"] == "0"
        assert os.environ["SDL_HINT_JOYSTICK_RAWINPUT"] == "0"
        assert os.environ["SDL_JOYSTICK_RAWINPUT"] == "0"
        assert SDL_FFB_SAFE_HINTS["SDL_HINT_JOYSTICK_HIDAPI_XBOX"] == "0"

    def test_dinput_source_never_sends_effects(self):
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "ptt" / "dinput.py")
        text = src.read_text(encoding="utf-8")
        assert "cooperative_level_flags()" in text
        assert "DISCL_NONEXCLUSIVE" in text
        assert "DISCL_BACKGROUND" in text
        assert "CreateEffect" not in text
        assert "SendForceFeedbackCommand" not in text
        assert "DIPROP_FFGAIN" not in text
        assert "DIPROP_AUTOCENTER" not in text
        assert "SetProperty" not in text
        assert "DISCL_EXCLUSIVE |" not in text
        assert "| DISCL_EXCLUSIVE" not in text
        assert "dev_vt[VT_DEV_SETPROPERTY]" not in text

    def test_pygame_source_never_inits_haptic(self):
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "ptt" / "pygame_joy.py")
        text = src.read_text(encoding="utf-8")
        assert "apply_sdl_ffb_safe_hints" in text
        assert "pygame.joystick.init()" in text
        assert "pygame.init(" not in text
        assert "haptic.init(" not in text
        assert "display.set_mode(" not in text

    def test_self_test_documents_no_haptic(self):
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "app.py")
        text = src.read_text(encoding="utf-8")
        assert "no haptic init" in text
        assert "pygame_no_haptic" in text
        assert "dinput_ffb_safe" in text
        assert "rawhid_ffb_safe" in text
        assert "apply_sdl_ffb_safe_hints" in text

    def test_settings_dialog_does_not_start_pygame_by_default(self):
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "ptt" / "settings_dialog.py")
        text = src.read_text(encoding="utf-8")
        assert "_maybe_start_pygame" in text
        assert "Wheel polling backend" in text
        # Opening the dialog must not unconditionally start pygame.
        assert "get_pygame_worker().ensure_started()" not in text.split(
            "def _maybe_start_pygame")[0]

    def test_wheel_backend_round_trip(self, tmp_path):
        from live_telemetry_evo import settings
        orig = settings._positions_path
        settings._positions_path = lambda: tmp_path / "s.json"
        try:
            settings.save_ptt_settings(wheel_backend="dinput")
            cfg = settings.load_ptt_settings()
            assert cfg["wheel_backend"] == "dinput"
        finally:
            settings._positions_path = orig

    def test_off_backend_does_not_start_pygame_worker(self):
        from live_telemetry_evo.ptt import input_binding as mod
        with patch.object(mod, "get_pygame_worker") as gw, \
             patch.object(mod, "_list_winmm", return_value=[]), \
             patch.object(mod, "_list_dinput", return_value=[]), \
             patch.object(mod, "_list_pygame", return_value=[]):
            listener = InputListener(wheel_backend="off")
            listener.start_assign(lambda _b: None, timeout_s=1)
            time.sleep(0.05)
            listener.stop()
        gw.assert_not_called()


import ctypes


class TestQuitOnLastWindowClosed:
    def test_quit_on_last_window_closed_is_false(self):
        """app.py must call app.setQuitOnLastWindowClosed(False) so
        closing dialogs while overlay is hidden doesn't quit."""
        import ast
        app_src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "app.py"
        source = app_src.read_text(encoding="utf-8")
        assert "setQuitOnLastWindowClosed(False)" in source, \
            "app.py must call setQuitOnLastWindowClosed(False)"


class TestResolveInputDevice:
    def test_prefers_name_over_index(self):
        from live_telemetry_evo.ptt.recorder import resolve_input_device
        wasapi = AudioDevice(
            index=7, name="Headset (Px7 S3 Hands-Free AG Audio)",
            max_input_channels=1, default_samplerate=16000,
            hostapi_name="Windows WASAPI")
        other = AudioDevice(
            index=0, name="Other", max_input_channels=1,
            default_samplerate=44100, is_default=True,
            hostapi_name="MME")
        with patch("live_telemetry_evo.ptt.recorder.list_input_devices",
                   return_value=[other, wasapi]):
            idx, err = resolve_input_device(
                "Headset (Px7 S3 Hands-Free AG Audio)",
                "Windows WASAPI", 0)
        assert err == ""
        assert idx == 7

    def test_no_default_minus_one(self):
        from live_telemetry_evo.ptt.recorder import resolve_input_device
        with patch("live_telemetry_evo.ptt.recorder.list_input_devices",
                   return_value=[]):
            idx, err = resolve_input_device("", "", None)
        assert idx is None
        assert "no input device" in err

    def test_recorder_refuses_portaudio_default_sentinel(self):
        rec = AudioRecorder(device_index=-1)
        assert rec.start() is False
        assert rec.last_error
        rec2 = AudioRecorder(device_index=None)
        assert rec2.start() is False
        assert rec2.last_error

    def test_recorder_from_settings_uses_name(self, tmp_path):
        from live_telemetry_evo.ptt.recorder import recorder_from_ptt_settings
        dev = AudioDevice(
            index=4, name="Headset (Px7 S3 Hands-Free AG Audio)",
            max_input_channels=1, default_samplerate=16000,
            hostapi_name="MME")
        with patch("live_telemetry_evo.ptt.recorder.list_input_devices",
                   return_value=[dev]):
            rec = recorder_from_ptt_settings({
                "device_name": "Headset (Px7 S3 Hands-Free AG Audio)",
                "device_hostapi": "MME",
                "device_index": 99,
            })
        assert rec._device == 4


class TestPTTHeldAndErrorCallbacks:
    def test_recorder_failure_notifies(self):
        held: list[bool] = []
        errors: list[str] = []
        rec = MagicMock()
        rec.start.return_value = False
        rec.last_error = "cannot open mic 'Headset' (index=4): boom"
        rec.stop.return_value = b"RIFF"
        ctrl = PTTController(
            recorder=rec,
            on_held=held.append,
            on_error=errors.append,
            mode=PTTMode.HOLD,
        )
        ctrl._on_press()
        assert held == [True, False]
        assert errors == ["cannot open mic 'Headset' (index=4): boom"]
        assert not ctrl.is_active


class TestWinmmBackgroundPoll:
    def test_bound_button_logs_down_up_once(self):
        from live_telemetry_evo.ptt import input_binding as mod
        events: list[str] = []
        logs: list[str] = []
        state = {"mask": 0}

        def mock_read(_jid):
            return state["mask"]

        def mock_list(*_a, **_k):
            return [WinmmJoystick(0, "Microsoft PC-joystick driver", 32)]

        with patch.object(mod, "_read_buttons_winmm", mock_read), \
             patch.object(mod, "list_joysticks_winmm", mock_list), \
             patch.object(mod, "log", side_effect=lambda m: logs.append(m)):
            listener = InputListener(wheel_backend="winmm")
            listener.set_binding(PTTBinding(
                type=BindingType.JOYSTICK, joy_id=0, button_id=24))
            listener.start(lambda: events.append("press"),
                           lambda: events.append("release"))
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and "winmm poll:" not in "\n".join(logs):
                time.sleep(0.01)
            state["mask"] = 1 << 24
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and "press" not in events:
                time.sleep(0.01)
            state["mask"] = 0
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and "release" not in events:
                time.sleep(0.01)
            listener.stop()
        assert events == ["press", "release"]
        joined = "\n".join(logs)
        assert joined.count("[ptt] down btn=24") == 1
        assert joined.count("[ptt] up") == 1

    def test_empty_device_list_keeps_polling(self):
        from live_telemetry_evo.ptt import input_binding as mod
        n = {"v": 0}

        def mock_list(*_a, **_k):
            n["v"] += 1
            return []

        with patch.object(mod, "list_joysticks_winmm", mock_list), \
             patch.object(mod, "_read_buttons_winmm", return_value=0):
            listener = InputListener(wheel_backend="winmm")
            listener.start(lambda: None, lambda: None)
            time.sleep(0.08)
            listener.stop()
        assert n["v"] >= 1


class TestRawHidBackend:
    def test_source_never_opens_or_writes_hid(self):
        from live_telemetry_evo.ptt.rawhid import rawhid_is_ffb_safe_source
        src = (Path(__file__).parent.parent / "src" / "live_telemetry_evo"
               / "ptt" / "rawhid.py")
        text = src.read_text(encoding="utf-8")
        assert rawhid_is_ffb_safe_source(text)
        assert "kernel32.CreateFile" not in text
        assert "RegisterRawInputDevices" in text
        assert "RIDI_PREPARSEDDATA" in text

    def test_usage_to_button_zero_based(self):
        from live_telemetry_evo.ptt.rawhid import _usage_to_button
        assert _usage_to_button(1) == 0
        assert _usage_to_button(25) == 24
        assert _usage_to_button(128) == 127

    def test_button_count_from_range_caps(self):
        from live_telemetry_evo.ptt.rawhid import (
            _HIDP_BUTTON_CAPS, _button_count_from_caps,
        )
        cap = _HIDP_BUTTON_CAPS()
        cap.IsRange = 1
        cap.Range.UsageMin = 1
        cap.Range.UsageMax = 128
        assert _button_count_from_caps([cap]) == 128

    def test_fake_backend_high_button(self):
        from live_telemetry_evo.ptt.rawhid import FakeRawHidBackend
        backend = FakeRawHidBackend()
        captured: list = []
        listener = InputListener(joy_backend=backend)
        listener.start_assign(captured.append)
        time.sleep(0.03)
        backend.emit(0, 64, True)
        deadline = time.monotonic() + 1.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.01)
        listener.stop()
        assert captured
        assert captured[0].button_id == 64

    def test_rawhid_is_a_wheel_backend(self):
        from live_telemetry_evo.ptt.ffb_safe import (
            WHEEL_BACKENDS, DEFAULT_WHEEL_BACKEND, normalize_wheel_backend,
        )
        assert DEFAULT_WHEEL_BACKEND == "winmm"
        assert "rawhid" in WHEEL_BACKENDS
        assert normalize_wheel_backend("rawinput") == "rawhid"

    def test_diagnostics_mentions_rawhid(self):
        report = run_diagnostics(active_backend="winmm")
        assert "rawhid" in report
        assert "128 buttons" in report or "128" in report


class TestPTTStartsWithoutIRacing:
    def test_app_restarts_ptt_at_startup_not_only_on_iracing(self):
        src = Path(__file__).parent.parent / "src" / "live_telemetry_evo" / "app.py"
        text = src.read_text(encoding="utf-8")
        assert "_restart_ptt()" in text
        assert "_init_ptt" not in text
        assert "PTT polling starts here" in text
        marker = text.find("PTT polling starts here")
        assert "_restart_ptt()" in text[marker:marker + 200]
        engineer = text.split("def _init_race_engineer", 1)[1].split(
            "def _on_rw_message", 1)[0]
        assert "_restart_ptt()" not in engineer
        assert "_init_ptt()" not in engineer
        assert "PTTStatusView" in text
        assert "event_type" in text
        assert '"type": "driver_feedback"' not in text
        assert "FeedbackDedupe" in text
        assert "configure_playback_from_ptt_settings" in text
        assert "idempotency_key" in text


class TestPreferEventType:
    def test_rewrites_type_alias(self):
        from live_telemetry_evo.race_worker_client import prefer_event_type
        out = prefer_event_type({"type": "driver_feedback", "message": "hi"})
        assert out["event_type"] == "driver_feedback"
        assert "type" not in out

    def test_event_type_wins(self):
        from live_telemetry_evo.race_worker_client import prefer_event_type
        out = prefer_event_type({
            "event_type": "spin",
            "type": "driver_feedback",
        })
        assert out["event_type"] == "spin"
        assert "type" not in out


class TestPostEventResult:
    def test_no_session_is_status_zero(self):
        from live_telemetry_evo.race_worker_client import RaceWorkerClient
        c = RaceWorkerClient(base_url="http://example.invalid", token="t")
        ok, code, err = c.post_event_result({"event_type": "driver_feedback"})
        assert ok is False
        assert code == 0
        assert "session" in err

    def test_http_error_returns_code(self):
        import urllib.error
        from live_telemetry_evo.race_worker_client import RaceWorkerClient
        c = RaceWorkerClient(base_url="http://example.invalid", token="t")
        c._session_id = "sid"
        err = urllib.error.HTTPError(
            "http://example.invalid", 422, "Unprocessable", {}, None)
        with patch("live_telemetry_evo.race_worker_client._request_ex",
                   side_effect=err):
            ok, code, msg = c.post_event_result({"type": "driver_feedback"})
        assert ok is False
        assert code == 422
        assert "422" in msg


class TestOutputDeviceListing:
    def test_find_output_by_name(self):
        stereo = AudioDevice(
            index=5, name="Headphones (Px7 S3 Stereo)",
            max_input_channels=0, max_output_channels=2,
            default_samplerate=48000, is_default=True,
            hostapi_name="Windows WASAPI")
        hfp = AudioDevice(
            index=6, name="Headset (Px7 S3 Hands-Free AG Audio)",
            max_input_channels=0, max_output_channels=1,
            default_samplerate=16000, hostapi_name="MME")
        with patch("live_telemetry_evo.ptt.recorder.list_output_devices",
                   return_value=[stereo, hfp]):
            idx = find_output_device_by_name(
                "Headphones (Px7 S3 Stereo)", "Windows WASAPI")
            assert idx == 5
            idx2, err = resolve_output_device(
                "Headphones (Px7 S3 Stereo)", "Windows WASAPI", 99)
            assert err == ""
            assert idx2 == 5

    def test_empty_selection_is_windows_default(self):
        idx, err = resolve_output_device("", "", None)
        assert idx is None
        assert err == ""

    def test_missing_output_name(self):
        with patch("live_telemetry_evo.ptt.recorder.list_output_devices",
                   return_value=[]):
            idx, err = resolve_output_device("Nope", "", None)
        assert idx is None
        assert "not found" in err


class TestFeedbackDedupe:
    def test_one_post_per_utterance(self):
        dedupe = FeedbackDedupe(window_s=4.0)
        posts: list[str] = []
        text = "баланс задний, недокрут в третьем"
        assert post_driver_feedback_once(text, posts.append, dedupe) is True
        assert post_driver_feedback_once(text, posts.append, dedupe) is False
        assert post_driver_feedback_once(
            "Баланс задний, недокрут в третьем!", posts.append, dedupe) is False
        assert posts == [text]

    def test_punctuation_and_case_normalise(self):
        assert normalize_transcript("Hello, World!") == "hello world"
        assert idempotency_key("Hello, World!") == idempotency_key("hello world")

    def test_different_utterances_both_post(self):
        dedupe = FeedbackDedupe(window_s=4.0)
        posts: list[str] = []
        assert post_driver_feedback_once("alpha", posts.append, dedupe)
        assert post_driver_feedback_once("bravo", posts.append, dedupe)
        assert posts == ["alpha", "bravo"]

    def test_same_text_after_window_posts_again(self):
        dedupe = FeedbackDedupe(window_s=0.05)
        posts: list[str] = []
        assert post_driver_feedback_once("same", posts.append, dedupe)
        time.sleep(0.08)
        assert post_driver_feedback_once("same", posts.append, dedupe)
        assert posts == ["same", "same"]

    def test_empty_rejected(self):
        dedupe = FeedbackDedupe()
        posts: list[str] = []
        assert post_driver_feedback_once("   ", posts.append, dedupe) is False
        assert posts == []

    def test_two_threads_one_post(self):
        dedupe = FeedbackDedupe(window_s=4.0)
        posts: list[str] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            post_driver_feedback_once("race", posts.append, dedupe)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert posts == ["race"]


class TestRecorderGraceAndClose:
    def test_stop_waits_for_late_samples(self):
        rec = AudioRecorder(device_index=0, grace_s=0.4, device_label="BT mic")
        rec._stream = object()
        rec._recording = True

        def deliver():
            time.sleep(0.05)
            rec._audio_callback(b"\x01\x00" * 32, 32, None, None)

        threading.Thread(target=deliver, daemon=True).start()
        wav = rec.stop()
        assert len(wav) > 44
        assert rec._stream is None

    def test_stop_closes_and_logs_hfp(self):
        rec = AudioRecorder(device_index=3, grace_s=0.0,
                            device_label="Headset (Px7 S3 Hands-Free AG Audio)")
        stream = MagicMock()
        rec._stream = stream
        logs: list[str] = []
        with patch("live_telemetry_evo.ptt.recorder.log",
                   side_effect=lambda m: logs.append(m)):
            rec.stop(grace_s=0.0)
        joined = "\n".join(logs)
        assert "closed mic promptly" in joined
        assert "HFP" in joined or "stereo" in joined.lower()
        stream.stop.assert_called()
        stream.close.assert_called()
        assert rec._stream is None

    def test_keep_warm_does_not_close(self):
        rec = AudioRecorder(device_index=1, grace_s=0.0, keep_warm=True,
                            device_label="BT mic")
        stream = MagicMock()
        rec._stream = stream
        rec.stop(grace_s=0.0)
        assert rec._stream is stream
        stream.stop.assert_not_called()
        rec.unwarm()
        assert rec._stream is None

    def test_controller_does_not_hold_lock_during_start(self):
        started = threading.Event()
        unblock = threading.Event()

        class SlowRec:
            last_error = ""

            def start(self):
                started.set()
                unblock.wait(timeout=1.0)
                return True

            def stop(self, grace_s=None):
                return b"RIFF" + b"\x00" * 100

        ctrl = PTTController(recorder=SlowRec(), mode=PTTMode.HOLD)
        t = threading.Thread(target=ctrl._on_press, daemon=True)
        t.start()
        assert started.wait(timeout=1.0)
        acquired = ctrl._lock.acquire(timeout=0.3)
        assert acquired, "PTT lock must not be held while the mic opens"
        ctrl._lock.release()
        unblock.set()
        t.join(timeout=1.0)
        assert ctrl.is_active
