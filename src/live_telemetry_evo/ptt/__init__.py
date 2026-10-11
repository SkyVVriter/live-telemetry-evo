"""Push-to-talk subsystem: audio capture, PTT binding, STT, intent dispatch.

Optional dependencies:
- ``sounddevice`` for audio capture
- ``pynput`` for keyboard PTT binding
- ``pygame`` for joystick/wheel button PTT binding
- ``openai`` for Whisper API STT
- ``faster-whisper`` for local STT
"""
from .recorder import AudioRecorder, list_input_devices
from .ptt_controller import PTTController, PTTMode
from .stt import STTEngine, get_stt_engine, list_stt_engines, register_stt_engine
from .intents import IntentHandler, DriverContext

__all__ = [
    "AudioRecorder",
    "list_input_devices",
    "PTTController",
    "PTTMode",
    "STTEngine",
    "get_stt_engine",
    "list_stt_engines",
    "register_stt_engine",
    "IntentHandler",
    "DriverContext",
]
