"""Per-lap summary builder.

Accumulates telemetry samples over a lap and produces a summary with
balance notes, incident delta, fuel usage, and sector analysis.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .balance import BalanceDetector, BalanceEvent, Phase
from .corner_map import TrackProfile
from .incidents import IncidentTracker, IncidentAlert

_PCT_PREFIX = "pct_"
_MERGE_GAP = 0.012  # ~1.2% of a lap — merge consecutive pct_ samples


def aggregate_balance_notes(events: list[BalanceEvent]) -> list[dict]:
    """Collapse per-sample events into one note per corner/phase.

    Without a track profile every sample is named ``pct_0.XXX``, which
    produced 700–1000 API notes per lap (one per 0.001 pct). Consecutive
    pct_ samples within ``_MERGE_GAP`` are merged; isolated pct_ clusters
    with fewer than two samples are dropped. Named corners are kept.
    Magnitude is the peak deficit scaled to 0–10.
    """
    if not events:
        return []

    ordered = sorted(events, key=lambda e: e.lap_dist_pct)
    clusters: list[list[BalanceEvent]] = []
    current: list[BalanceEvent] = []

    def _named(name: str) -> bool:
        return not name.startswith(_PCT_PREFIX)

    for ev in ordered:
        if not current:
            current = [ev]
            continue
        prev = current[-1]
        same_named = _named(ev.corner_name) and ev.corner_name == prev.corner_name
        close_pct = (
            not _named(ev.corner_name)
            and not _named(prev.corner_name)
            and (ev.lap_dist_pct - prev.lap_dist_pct) <= _MERGE_GAP
        )
        if same_named or close_pct:
            current.append(ev)
        else:
            clusters.append(current)
            current = [ev]
    if current:
        clusters.append(current)

    notes: list[dict] = []
    unnamed_idx = 0
    for cluster in clusters:
        named = [e for e in cluster if _named(e.corner_name)]
        if named:
            corner = named[0].corner_name
        else:
            if len(cluster) < 2:
                continue
            unnamed_idx += 1
            corner = f"T{unnamed_idx}"

        by_phase: dict[Phase, list[BalanceEvent]] = defaultdict(list)
        for e in cluster:
            by_phase[e.phase].append(e)

        for phase in (Phase.ENTRY, Phase.MID, Phase.EXIT):
            evs = by_phase.get(phase)
            if not evs:
                continue
            dominant = Counter(e.balance_type for e in evs).most_common(1)[0][0]
            peak = max(e.magnitude for e in evs)
            mag = min(10.0, round(peak * 10.0, 1))
            if mag <= 0:
                continue
            notes.append({
                "corner": corner,
                "phase": phase.value,
                "type": dominant.value,
                "magnitude": mag,
            })
    return notes


@dataclass
class LapSummary:
    lap_number: int = 0
    lap_time: float = 0.0
    position: int = 0
    class_position: int = 0
    incidents_total: int = 0
    incidents_this_lap: int = 0
    fuel_used: float = 0.0
    fuel_remaining: float = 0.0
    out_lap: bool = False
    balance_events: list[BalanceEvent] = field(default_factory=list)
    incident_alerts: list[IncidentAlert] = field(default_factory=list)
    top_speed: float = 0.0
    min_speed: float = 999.0
    off_track: bool = False
    pit_this_lap: bool = False

    def dominant_balance(self) -> str:
        if not self.balance_events:
            return "neutral"
        counts = Counter(e.balance_type.value for e in self.balance_events)
        return counts.most_common(1)[0][0]

    def balance_notes_for_api(self) -> list[dict]:
        """One note per corner/phase with peak magnitude (no per-sample spam)."""
        return aggregate_balance_notes(self.balance_events)

    def was_driven(self) -> bool:
        """True when this summary has any on-track evidence."""
        return self.top_speed > 0 or self.pit_this_lap or self.fuel_remaining > 0


class LapSummaryBuilder:
    """Accumulates telemetry samples and produces per-lap summaries."""

    def __init__(
        self,
        balance_detector: BalanceDetector,
        incident_tracker: IncidentTracker,
        track_profile: Optional[TrackProfile] = None,
    ) -> None:
        self.balance = balance_detector
        self.incidents = incident_tracker
        self.track_profile = track_profile
        self._current = LapSummary()
        self._start_fuel: float = 0.0
        self._prev_incidents: int = 0

    def new_lap(self, lap_number: int, fuel: float, incidents: int,
                position: int = 0, class_position: int = 0) -> None:
        self._current = LapSummary(
            lap_number=lap_number,
            position=position if position > 0 else 0,
            class_position=class_position if class_position > 0 else 0,
        )
        self._start_fuel = fuel
        self._prev_incidents = incidents

    def add_sample(
        self,
        speed_kmh: float,
        steer_rad: float,
        yaw_rate: float,
        brake: float,
        throttle: float,
        lap_dist_pct: float,
        fuel: float,
        incidents: int,
        on_pit_road: bool = False,
        position: int = 0,
        class_position: int = 0,
    ) -> None:
        s = self._current
        s.top_speed = max(s.top_speed, speed_kmh)
        if speed_kmh > 5:
            s.min_speed = min(s.min_speed, speed_kmh)

        if on_pit_road:
            s.pit_this_lap = True
        if position > 0:
            s.position = position
        if class_position > 0:
            s.class_position = class_position

        corner = self.track_profile.corner_at(lap_dist_pct) if self.track_profile else None
        event = self.balance.classify(
            speed_kmh, steer_rad, yaw_rate, brake, throttle,
            corner=corner, lap_dist_pct=lap_dist_pct,
        )
        if event:
            s.balance_events.append(event)

        inc_alerts = self.incidents.update(incidents)
        s.incident_alerts.extend(inc_alerts)

        s.incidents_total = incidents
        s.incidents_this_lap = incidents - self._prev_incidents
        s.fuel_remaining = fuel

    def finish_lap(self, lap_time: float = 0.0,
                   out_lap: bool | None = None) -> LapSummary:
        s = self._current
        s.lap_time = lap_time
        s.out_lap = bool(lap_time <= 0.0) if out_lap is None else bool(out_lap)
        s.fuel_used = max(0.0, self._start_fuel - s.fuel_remaining)
        if s.min_speed > 900:
            s.min_speed = 0.0
        return s

    def current_summary(self) -> LapSummary:
        return self._current
