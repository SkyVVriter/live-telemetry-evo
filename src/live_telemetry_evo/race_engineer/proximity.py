"""Faster-car-behind detector.

Uses the per-car position/lap-distance data from iRacing to detect when
a car in a higher overall position is closing from behind, generating a
warning for the driver.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..telemetry import CarPosition


@dataclass
class ProximityAlert:
    car_number: str
    driver_name: str
    gap_pct: float
    closing_rate: float
    message: str


class FasterCarBehindDetector:
    """Detect faster cars approaching from behind.

    Uses lap-distance percentages and estimated times to determine if a
    car behind is closing on the player.
    """

    def __init__(self, alert_gap_pct: float = 0.03,
                 closing_threshold: float = 0.001) -> None:
        self.alert_gap_pct = alert_gap_pct
        self.closing_threshold = closing_threshold
        self._prev_gaps: dict[int, float] = {}
        self._alerted: set[int] = set()

    def update(self, player_pct: float, player_position: int,
               cars: list[CarPosition]) -> list[ProximityAlert]:
        """Check all cars behind. Returns new alerts for closing cars."""
        alerts: list[ProximityAlert] = []

        for car in cars:
            if car.position <= 0 or car.position >= player_position:
                continue
            if car.car_idx in self._alerted:
                continue

            gap = player_pct - car.lap_dist_pct
            if gap < 0:
                gap += 1.0

            if gap > 0.5:
                continue

            prev_gap = self._prev_gaps.get(car.car_idx)
            self._prev_gaps[car.car_idx] = gap

            if prev_gap is None:
                continue

            closing_rate = prev_gap - gap

            if gap <= self.alert_gap_pct and closing_rate > self.closing_threshold:
                self._alerted.add(car.car_idx)
                name = car.driver_name or f"#{car.car_number}"
                alerts.append(ProximityAlert(
                    car_number=car.car_number,
                    driver_name=car.driver_name,
                    gap_pct=gap,
                    closing_rate=closing_rate,
                    message=f"Быстрый позади: {name} (P{car.position})",
                ))

        stale = [idx for idx, g in self._prev_gaps.items() if g > 0.15]
        for idx in stale:
            self._prev_gaps.pop(idx, None)
            self._alerted.discard(idx)

        return alerts

    def reset(self) -> None:
        self._prev_gaps.clear()
        self._alerted.clear()
