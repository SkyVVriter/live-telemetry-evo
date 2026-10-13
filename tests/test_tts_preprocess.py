"""TTS text preprocessor: enumerations and spoken lap times."""
from __future__ import annotations

import time

from live_telemetry_evo.tts.base import Priority, VoiceEngine
from live_telemetry_evo.tts.preprocess import (
    SSML_BREAK,
    number_to_words,
    preprocess_tts_text,
    wrap_ssml,
)
from live_telemetry_evo.tts.scheduler import SpeechScheduler


class _MockEngine(VoiceEngine):
    name = "mock"

    def __init__(self) -> None:
        self.spoken: list[tuple[str, Priority]] = []

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        self.spoken.append((text, priority))

    def list_voices(self):
        return []

    def settings_schema(self):
        return {}


class TestNumberWords:
    def test_basic(self):
        assert number_to_words(0) == "zero"
        assert number_to_words(1) == "one"
        assert number_to_words(22) == "twenty-two"
        assert number_to_words(53) == "fifty-three"
        assert number_to_words(10) == "ten"


class TestLapTimes:
    def test_one_twenty_two(self):
        assert preprocess_tts_text("1:22") == "one twenty-two"
        assert preprocess_tts_text("last lap 1:22") == "last lap one twenty-two"

    def test_oh_seconds(self):
        assert preprocess_tts_text("1:05") == "one oh five"

    def test_with_fraction(self):
        assert preprocess_tts_text("1:53.109") == (
            "one fifty-three point one oh nine"
        )

    def test_leaves_unrelated_colons(self):
        assert "note:" in preprocess_tts_text("note: hold the line")


class TestEnumerations:
    def test_turns_two_five_and_six(self):
        out = preprocess_tts_text("the car is loose in turns 2, 5 and 6")
        assert SSML_BREAK in out
        assert out == (
            f"the car is loose in turns 2 {SSML_BREAK} 5 {SSML_BREAK} 6"
        )

    def test_oxford_comma_and_ampersand(self):
        assert preprocess_tts_text("corners 1, 3, and 8") == (
            f"corners 1 {SSML_BREAK} 3 {SSML_BREAK} 8"
        )
        assert preprocess_tts_text("laps 2 & 4") == (
            f"laps 2 {SSML_BREAK} 4"
        )

    def test_single_turn_unchanged(self):
        assert preprocess_tts_text("understeer in turn 4") == (
            "understeer in turn 4"
        )

    def test_idempotent(self):
        once = preprocess_tts_text("turns 2, 5 and 6")
        assert preprocess_tts_text(once) == once


class TestWrapSsml:
    def test_wraps_only_when_break_present(self):
        assert wrap_ssml("hello") == "hello"
        broken = f"turns 2 {SSML_BREAK} 5"
        assert wrap_ssml(broken).startswith("<speak>")
        assert SSML_BREAK in wrap_ssml(broken)


class TestSchedulerUsesPreprocessor:
    def test_spoken_text_is_preprocessed(self):
        engine = _MockEngine()
        sched = SpeechScheduler(engine)
        sched.start()
        sched.enqueue("last lap 1:22, watch turns 2, 5 and 6", Priority.URGENT)
        time.sleep(0.4)
        sched.stop()
        assert engine.spoken
        spoken = engine.spoken[0][0]
        assert "one twenty-two" in spoken
        assert SSML_BREAK in spoken
        assert "1:22" not in spoken
