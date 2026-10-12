"""Tests for the PTT (Push-to-Talk) subsystem with mocked audio/input.

All tests run headless — sounddevice, pynput, and pygame are mocked.
"""
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from live_telemetry_evo.ptt.recorder import (
    AudioRecorder, _pcm_to_wav, list_input_devices, find_device_by_name,
    AudioDevice,
)
from live_telemetry_evo.ptt.ptt_controller import (
    PTTController, PTTMode, _generate_beep, _MIN_HOLD_DURATION_S,
)
from live_telemetry_evo.ptt.input_binding import (
    PTTBinding, BindingType, InputListener,
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

class TestWinmmBackend:
    """Test the winmm joystick backend with mocked ctypes."""

    def test_joycapsw_struct_fields(self):
        from live_telemetry_evo.ptt.input_binding import _JOYCAPSW
        caps = _JOYCAPSW()
        assert hasattr(caps, "szPname")
        assert hasattr(caps, "wNumButtons")
        assert hasattr(caps, "wMid")

    def test_joyinfoex_struct_fields(self):
        from live_telemetry_evo.ptt.input_binding import _JOYINFOEX
        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(_JOYINFOEX)
        assert hasattr(info, "dwButtons")
        assert hasattr(info, "dwFlags")
        assert info.dwSize > 0

    def test_list_joysticks_winmm_with_mock(self):
        """list_joysticks_winmm returns devices when winmm reports them."""
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.input_binding import (
            _JOYCAPSW, WinmmJoystick, _JOYERR_NOERROR,
        )

        mock_caps = _JOYCAPSW()
        mock_caps.szPname = "Moza R16"
        mock_caps.wNumButtons = 12

        def mock_joyGetNumDevs():
            return 2

        def mock_joyGetDevCapsW(joy_id, caps_ptr, size):
            if joy_id == 0:
                ctypes.memmove(caps_ptr, ctypes.byref(mock_caps),
                               min(size, ctypes.sizeof(_JOYCAPSW)))
                return _JOYERR_NOERROR
            return 167  # JOYERR_PARMS

        orig = mod._winmm_funcs
        mod._winmm_funcs = (mock_joyGetNumDevs, mock_joyGetDevCapsW, None)
        try:
            from live_telemetry_evo.ptt.input_binding import list_joysticks_winmm
            joys = list_joysticks_winmm()
            assert len(joys) == 1
            assert joys[0].name == "Moza R16"
            assert joys[0].num_buttons == 12
            assert joys[0].index == 0
        finally:
            mod._winmm_funcs = orig

    def test_list_joysticks_winmm_empty(self):
        """list_joysticks_winmm returns empty when no devices."""
        from live_telemetry_evo.ptt import input_binding as mod
        orig = mod._winmm_funcs
        mod._winmm_funcs = (lambda: 0, lambda *a: 167, None)
        try:
            from live_telemetry_evo.ptt.input_binding import list_joysticks_winmm
            assert list_joysticks_winmm() == []
        finally:
            mod._winmm_funcs = orig

    def test_read_buttons_winmm_with_mock(self):
        """_read_buttons_winmm reads the button bitmask correctly."""
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.input_binding import (
            _JOYINFOEX, _JOYERR_NOERROR, _read_buttons_winmm,
        )

        def mock_joyGetPosEx(joy_id, info_ptr):
            info = ctypes.cast(info_ptr, ctypes.POINTER(_JOYINFOEX)).contents
            info.dwButtons = 0b1010  # buttons 1 and 3 pressed
            return _JOYERR_NOERROR

        orig = mod._winmm_funcs
        mod._winmm_funcs = (None, None, mock_joyGetPosEx)
        try:
            bitmask = _read_buttons_winmm(0)
            assert bitmask & (1 << 1)  # button 1 pressed
            assert bitmask & (1 << 3)  # button 3 pressed
            assert not (bitmask & (1 << 0))  # button 0 not pressed
        finally:
            mod._winmm_funcs = orig

    def test_read_buttons_returns_zero_on_error(self):
        """_read_buttons_winmm returns 0 when winmm is unavailable."""
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.input_binding import _read_buttons_winmm
        orig = mod._winmm_funcs
        mod._winmm_funcs = False
        try:
            assert _read_buttons_winmm(0) == 0
        finally:
            mod._winmm_funcs = orig

    def test_list_joysticks_prefers_winmm(self):
        """list_joysticks uses winmm when available, not pygame."""
        from live_telemetry_evo.ptt import input_binding as mod
        from live_telemetry_evo.ptt.input_binding import (
            _JOYCAPSW, _JOYERR_NOERROR,
        )

        mock_caps = _JOYCAPSW()
        mock_caps.szPname = "Test Wheel"
        mock_caps.wNumButtons = 8

        def mock_num():
            return 1

        def mock_caps_fn(jid, ptr, sz):
            if jid == 0:
                ctypes.memmove(ptr, ctypes.byref(mock_caps),
                               min(sz, ctypes.sizeof(_JOYCAPSW)))
                return _JOYERR_NOERROR
            return 167

        orig = mod._winmm_funcs
        mod._winmm_funcs = (mock_num, mock_caps_fn, None)
        try:
            result = mod.list_joysticks()
            assert len(result) == 1
            assert result[0]["name"] == "Test Wheel"
        finally:
            mod._winmm_funcs = orig

    def test_winmm_joystick_dataclass(self):
        from live_telemetry_evo.ptt.input_binding import WinmmJoystick
        j = WinmmJoystick(index=0, name="Moza R16", num_buttons=24)
        assert j.index == 0
        assert j.name == "Moza R16"
        assert j.num_buttons == 24


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
