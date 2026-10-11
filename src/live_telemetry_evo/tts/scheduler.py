"""Speech scheduler: queue with priorities, timing awareness.

Queues messages and speaks them at appropriate moments:
- Urgent messages are spoken immediately.
- Normal/low messages wait for straights (low steering + not braking,
  or LapDistPct zones from the track profile) unless they expire first.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .base import Priority, VoiceEngine


@dataclass(order=True)
class SpeechMessage:
    """A queued speech message with priority and expiry."""
    priority: int = field(compare=True)
    timestamp: float = field(compare=True, default_factory=time.monotonic)
    text: str = field(compare=False, default="")
    expires_at: float = field(compare=False, default=0.0)

    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.monotonic() > self.expires_at


class SpeechScheduler:
    """Priority-aware speech scheduler with driving-context awareness.

    Parameters:
        engine: The TTS engine to use for playback.
        is_on_straight: Callable returning True when the car is on a
            straight (safe to speak). If None, always speaks.
        max_queue: Maximum queue size; oldest low-priority messages are
            dropped when exceeded.
    """

    def __init__(
        self,
        engine: VoiceEngine,
        is_on_straight: Optional[Callable[[], bool]] = None,
        max_queue: int = 20,
    ) -> None:
        self.engine = engine
        self.is_on_straight = is_on_straight
        self.max_queue = max_queue
        self._queue: list[SpeechMessage] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._speaking = False

    def enqueue(self, text: str, priority: Priority = Priority.NORMAL,
                ttl_seconds: float = 30.0) -> None:
        """Add a message to the queue."""
        msg = SpeechMessage(
            priority=-priority.value,  # negate so higher priority sorts first
            text=text,
            expires_at=time.monotonic() + ttl_seconds if ttl_seconds > 0 else 0.0,
        )
        with self._lock:
            self._queue.append(msg)
            self._queue.sort()
            while len(self._queue) > self.max_queue:
                self._queue.pop()  # drop lowest priority (last)

    def start(self) -> None:
        """Start the scheduler loop on a background thread."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="tts-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the scheduler and drain the queue."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        with self._lock:
            self._queue.clear()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def _run(self) -> None:
        while not self._stop.is_set():
            msg = self._pick_message()
            if msg is not None:
                self._speaking = True
                try:
                    self.engine.speak(msg.text, Priority(-msg.priority))
                except Exception:
                    pass
                self._speaking = False
            else:
                self._stop.wait(0.1)

    def _pick_message(self) -> Optional[SpeechMessage]:
        """Pick the next message to speak, respecting driving context."""
        with self._lock:
            self._queue = [m for m in self._queue if not m.is_expired()]
            if not self._queue:
                return None

            first = self._queue[0]
            is_urgent = (-first.priority) >= Priority.URGENT.value

            if is_urgent:
                return self._queue.pop(0)

            on_straight = True
            if self.is_on_straight is not None:
                try:
                    on_straight = self.is_on_straight()
                except Exception:
                    on_straight = True

            if on_straight:
                return self._queue.pop(0)

            return None

    @property
    def is_speaking(self) -> bool:
        return self._speaking
