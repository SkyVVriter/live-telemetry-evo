"""Idempotent driver_feedback posting: exactly one POST per utterance.

A Bluetooth PTT bounce (or a queued retry plus a live post) previously
produced two ``POST /events`` 201s ~1 s apart for the same spoken line.
This module normalises the transcript and drops repeats inside a short
window.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Callable

from ..logbook import log

DEFAULT_WINDOW_S = 4.0


def normalize_transcript(text: str) -> str:
    """Case-fold, strip punctuation, collapse whitespace."""
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def idempotency_key(text: str) -> str:
    """Stable key for a normalised transcript (not time-bucketed)."""
    norm = normalize_transcript(text)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


class FeedbackDedupe:
    """Return True at most once per normalised transcript per window.

    Thread-safe. Empty transcripts are never accepted.
    """

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self._last_key: str = ""
        self._last_t: float = 0.0
        self._seen: dict[str, float] = {}

    def accept(self, text: str, *, now: float | None = None) -> bool:
        """True if this utterance should be POSTed."""
        key = idempotency_key(text)
        if not key:
            return False
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(t)
            last = self._seen.get(key)
            if last is not None and (t - last) < self.window_s:
                log(f"[ptt] skip duplicate driver_feedback key={key} "
                    f"dt={t - last:.2f}s")
                return False
            if self._last_key == key and (t - self._last_t) < self.window_s:
                log(f"[ptt] skip duplicate driver_feedback key={key}")
                return False
            self._seen[key] = t
            self._last_key = key
            self._last_t = t
            return True

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s * 2
        stale = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in stale:
            del self._seen[k]


def post_driver_feedback_once(
    text: str,
    post: Callable[[str], None],
    dedupe: FeedbackDedupe,
) -> bool:
    """Call ``post(text)`` at most once per utterance. Returns True if posted."""
    if not dedupe.accept(text):
        return False
    post(text)
    return True
