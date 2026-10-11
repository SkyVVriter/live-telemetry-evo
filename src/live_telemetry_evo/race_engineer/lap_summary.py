"""Per-lap summary builder.

Accumulates telemetry samples over a lap and produces a summary with
balance notes, incident delta, fuel usage, and sector analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .balance import BalanceDetector, BalanceEvent, Phase
from .corner_map import Corner, TrackProfile
from .incidents import IncidentTracker, IncidentAlert


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
    balance_events: list[BalanceEvent] = field(default_factory=list)
    incident_alerts: list[IncidentAlert] = field(default_factory=list)
    top_speed: float = 0.0
    min_speed: float = 999.0
    off_track: bool = False
    pit_this_lap: bool = False

    def dominant_balance(self) -> str:
        if not self.balance_events:
            return "neutral"
        from collections import Counter
        counts = Counter(e.balance_type.value for e in self.balance_events)
        return counts.most_common(1)[0][0]

    def balance_notes_for_api(self) -> list[dict]:
        """Format balance events for the race-worker API."""
        from collections import defaultdict
        by_corner: dict[str, list[BalanceEvent]] = defaultdict(list)
        for e in self.balance_events:
            by_corner[e.corner_name].append(e)

        notes = []
        for corner_name, events in by_corner.items():
            from collections import Counter
            phase_counts: dict[str, Counter] = {}
            for e in events:
                key = e.phase.value
                if key not in phase_counts:
                    phase_counts[key] = Counter()
                phase_counts[key][e.balance_type.value] += 1

            for phase_str, counter in phase_counts.items():
                dominant = counter.most_common(1)[0]
                if dominant[1] >= 2:
                    mags = [e.magnitude for e in events
                            if e.phase.value == phase_str
                            and e.balance_type.value == dominant[0]]
                    avg_mag = sum(mags) / len(mags) if mags else 0.0
                    notes.append({
                        "corner": corner_name,
                        "phase": phase_str,
                        "type": dominant[0],
                        "magnitude": round(min(avg_mag * 10, 10.0), 1),
                    })
        return notes


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
            position=position,
            class_position=class_position,
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
    ) -> None:
        s = self._current
        s.top_speed = max(s.top_speed, speed_kmh)
        if speed_kmh > 5:
            s.min_speed = min(s.min_speed, speed_kmh)

        if on_pit_road:
            s.pit_this_lap = True

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

    def finish_lap(self, lap_time: float = 0.0) -> LapSummary:
        s = self._current
        s.lap_time = lap_time
        s.fuel_used = max(0.0, self._start_fuel - s.fuel_remaining)
        if s.min_speed > 900:
            s.min_speed = 0.0
        return s

    def current_summary(self) -> LapSummary:
        return self._current
