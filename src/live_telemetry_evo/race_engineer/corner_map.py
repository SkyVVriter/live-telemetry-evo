"""Corner map auto-detection and track profile support.

Builds a list of corners from the first clean laps by finding speed
minima co-located with significant steering input. A track-profile JSON
file can override the auto-detected map with human-curated corner names,
LapDistPct ranges, and focus flags.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Corner:
    name: str
    start_pct: float
    apex_pct: float
    end_pct: float
    focus: bool = False


@dataclass
class TrackProfile:
    track_id: str = ""
    track_name: str = ""
    corners: list[Corner] = field(default_factory=list)

    def corner_at(self, pct: float) -> Optional[Corner]:
        for c in self.corners:
            if c.start_pct <= pct <= c.end_pct:
                return c
        return None


def load_track_profile(path: Path) -> Optional[TrackProfile]:
    """Load a track profile JSON. Returns None if the file is missing or
    invalid.

    Expected format::

        {
            "track_id": "274",
            "track_name": "Spa-Francorchamps",
            "corners": [
                {"name": "La Source", "start_pct": 0.01, "apex_pct": 0.02,
                 "end_pct": 0.04, "focus": true},
                ...
            ]
        }
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        corners = []
        for c in data.get("corners", []):
            corners.append(Corner(
                name=c["name"],
                start_pct=float(c["start_pct"]),
                apex_pct=float(c.get("apex_pct", c["start_pct"])),
                end_pct=float(c["end_pct"]),
                focus=bool(c.get("focus", False)),
            ))
        return TrackProfile(
            track_id=str(data.get("track_id", "")),
            track_name=data.get("track_name", ""),
            corners=corners,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


@dataclass
class _SpeedSample:
    pct: float
    speed: float
    steer: float


def detect_corners(speed_samples: list[tuple[float, float, float]],
                   min_speed_drop: float = 20.0,
                   min_steer_rad: float = 0.05,
                   merge_pct: float = 0.02) -> list[Corner]:
    """Auto-detect corners from a list of ``(lap_dist_pct, speed_kmh, steer_rad)``
    samples (one clean lap).

    Algorithm:
    1. Find local speed minima where speed drops by at least ``min_speed_drop``
       km/h from the preceding local maximum.
    2. Filter to points where ``|steering| > min_steer_rad``.
    3. Merge nearby minima within ``merge_pct`` of lap distance.
    4. Expand each apex into a corner zone using the speed rise on both sides.
    """
    if len(speed_samples) < 10:
        return []

    samples = [_SpeedSample(p, s, st) for p, s, st in speed_samples]
    samples.sort(key=lambda s: s.pct)

    minima: list[_SpeedSample] = []
    for i in range(1, len(samples) - 1):
        prev_s, cur_s, next_s = samples[i - 1], samples[i], samples[i + 1]
        if cur_s.speed <= prev_s.speed and cur_s.speed <= next_s.speed:
            if abs(cur_s.steer) >= min_steer_rad:
                look_back_max = max((s.speed for s in samples[max(0, i - 20):i]),
                                    default=cur_s.speed)
                if look_back_max - cur_s.speed >= min_speed_drop:
                    minima.append(cur_s)

    merged: list[_SpeedSample] = []
    for m in minima:
        if merged and abs(m.pct - merged[-1].pct) < merge_pct:
            if m.speed < merged[-1].speed:
                merged[-1] = m
        else:
            merged.append(m)

    corners: list[Corner] = []
    for idx, apex in enumerate(merged):
        apex_i = next((i for i, s in enumerate(samples) if s.pct >= apex.pct), 0)
        start_i = apex_i
        for j in range(apex_i - 1, -1, -1):
            if samples[j].speed > apex.speed * 1.15:
                start_i = j
                break
        end_i = apex_i
        for j in range(apex_i + 1, len(samples)):
            if samples[j].speed > apex.speed * 1.15:
                end_i = j
                break

        corners.append(Corner(
            name=f"T{idx + 1}",
            start_pct=samples[start_i].pct,
            apex_pct=apex.pct,
            end_pct=samples[end_i].pct,
        ))

    return corners
