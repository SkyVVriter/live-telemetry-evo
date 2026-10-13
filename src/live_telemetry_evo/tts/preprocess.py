"""Normalize race-engineer phrases before they hit a TTS engine.

Splits number enumerations (``turns 2, 5 and 6``) with an SSML break
and reads lap times like ``1:22`` as ``one twenty-two``.
"""
from __future__ import annotations

import re

SSML_BREAK = '<break time="0.3s"/>'

_ONES = (
    "zero", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine",
)
_TEENS = (
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety",
)

# 1:22, 1:53.109, 12:04.3 — minutes : seconds [fraction]
_LAP_TIME_RE = re.compile(
    r"(?<![\d:])(?P<min>\d{1,2}):(?P<sec>[0-5]\d)(?:[.,](?P<frac>\d{1,3}))?(?![\d:])"
)

# "turns 2, 5 and 6" / "corner 1, 3, and 8" / "laps 2 & 4"
_ENUM_HEAD = r"(?i)\b(?P<head>turns?|corners?|laps?)\s+"
_ENUM_ITEM = r"\d+"
_ENUM_SEP = r"(?:\s*,\s*|\s+(?:and|&)\s+|\s*,\s*(?:and|&)\s+)"
_ENUM_RE = re.compile(
    _ENUM_HEAD + rf"(?P<list>{_ENUM_ITEM}(?:{_ENUM_SEP}{_ENUM_ITEM})+)"
)


def number_to_words(n: int) -> str:
    """English words for 0–99 (lap minutes/seconds)."""
    n = int(n)
    if n < 0:
        return str(n)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        if ones == 0:
            return _TENS[tens]
        return f"{_TENS[tens]}-{_ONES[ones]}"
    return str(n)


def _seconds_words(sec: int) -> str:
    if sec < 10:
        return f"oh {_ONES[sec]}"
    return number_to_words(sec)


def _frac_words(frac: str) -> str:
    parts = []
    for ch in frac:
        if ch == "0":
            parts.append("oh")
        elif ch.isdigit():
            parts.append(_ONES[int(ch)])
    return " ".join(parts)


def _lap_time_repl(match: re.Match[str]) -> str:
    minutes = int(match.group("min"))
    seconds = int(match.group("sec"))
    spoken = f"{number_to_words(minutes)} {_seconds_words(seconds)}"
    frac = match.group("frac")
    if frac:
        spoken = f"{spoken} point {_frac_words(frac)}"
    return spoken


def _enum_repl(match: re.Match[str]) -> str:
    head = match.group("head")
    nums = re.findall(r"\d+", match.group("list"))
    return head + " " + f" {SSML_BREAK} ".join(nums)


def preprocess_tts_text(text: str) -> str:
    """Return speech-ready text (SSML breaks + spoken lap times)."""
    if not text:
        return text
    text = _LAP_TIME_RE.sub(_lap_time_repl, text)
    text = _ENUM_RE.sub(_enum_repl, text)
    return text


def wrap_ssml(text: str) -> str:
    """Wrap text that already contains SSML breaks in a ``<speak>`` root."""
    if "<break" not in text:
        return text
    if text.lstrip().startswith("<speak"):
        return text
    escaped = text.replace("&", "&amp;")
    return f"<speak>{escaped}</speak>"
