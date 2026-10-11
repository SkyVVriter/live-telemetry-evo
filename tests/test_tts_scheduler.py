"""Tests for the TTS scheduler with a mock engine."""
import time
import threading

import pytest

from live_telemetry_evo.tts.base import Priority, VoiceEngine
from live_telemetry_evo.tts.scheduler import SpeechMessage, SpeechScheduler


class MockEngine(VoiceEngine):
    """Mock TTS engine that records what was spoken."""
    name = "mock"

    def __init__(self):
        self.spoken: list[tuple[str, Priority]] = []
        self._lock = threading.Lock()

    def speak(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        with self._lock:
            self.spoken.append((text, priority))
        time.sleep(0.01)

    def list_voices(self):
        return [{"id": "mock", "name": "Mock Voice"}]

    def settings_schema(self):
        return {}


class TestSpeechMessage:
    def test_expiry(self):
        msg = SpeechMessage(priority=0, text="test",
                            expires_at=time.monotonic() - 1.0)
        assert msg.is_expired()

    def test_not_expired(self):
        msg = SpeechMessage(priority=0, text="test",
                            expires_at=time.monotonic() + 60.0)
        assert not msg.is_expired()

    def test_no_expiry(self):
        msg = SpeechMessage(priority=0, text="test", expires_at=0.0)
        assert not msg.is_expired()


class TestSpeechScheduler:
    def test_enqueue_and_speak(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine)
        sched.start()
        sched.enqueue("hello", Priority.NORMAL)
        time.sleep(0.5)
        sched.stop()
        assert len(engine.spoken) == 1
        assert engine.spoken[0][0] == "hello"

    def test_priority_ordering(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine)
        sched.enqueue("low", Priority.LOW)
        sched.enqueue("urgent", Priority.URGENT)
        sched.enqueue("normal", Priority.NORMAL)
        sched.start()
        time.sleep(1.0)
        sched.stop()
        assert len(engine.spoken) == 3
        assert engine.spoken[0][0] == "urgent"

    def test_expired_messages_dropped(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine)
        sched.enqueue("expired", Priority.LOW, ttl_seconds=0.01)
        time.sleep(0.1)
        sched.start()
        time.sleep(0.5)
        sched.stop()
        assert not any(t == "expired" for t, _ in engine.spoken)

    def test_on_straight_gating(self):
        engine = MockEngine()
        on_straight_flag = [False]
        sched = SpeechScheduler(engine,
                                is_on_straight=lambda: on_straight_flag[0])
        sched.enqueue("wait for straight", Priority.NORMAL)
        sched.start()
        time.sleep(0.3)
        assert len(engine.spoken) == 0  # not on straight
        on_straight_flag[0] = True
        time.sleep(0.5)
        sched.stop()
        assert len(engine.spoken) == 1

    def test_urgent_ignores_straight(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine, is_on_straight=lambda: False)
        sched.enqueue("urgent msg", Priority.URGENT)
        sched.start()
        time.sleep(0.5)
        sched.stop()
        assert len(engine.spoken) == 1
        assert engine.spoken[0][0] == "urgent msg"

    def test_max_queue_overflow(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine, is_on_straight=lambda: False,
                                max_queue=3)
        for i in range(10):
            sched.enqueue(f"msg-{i}", Priority.LOW)
        assert sched.pending_count <= 3

    def test_stop_clears_queue(self):
        engine = MockEngine()
        sched = SpeechScheduler(engine, is_on_straight=lambda: False)
        sched.enqueue("a", Priority.LOW)
        sched.enqueue("b", Priority.LOW)
        sched.start()
        sched.stop()
        assert sched.pending_count == 0
