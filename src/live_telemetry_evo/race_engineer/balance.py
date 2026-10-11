"""Balance detector: classify understeer / oversteer / slide by phase.

Compares actual yaw rate against expected (bicycle model) to determine
the car's handling balance at each telemetry sample. The detector learns
a per-corner baseline from the driver's clean laps and reports deviations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .corner_map import Corner


class BalanceType(Enum):
    NEUTRAL = "neutral"
    UNDERSTEER = "understeer"
    OVERSTEER = "oversteer"
    SLIDE = "slide"


class Phase(Enum):
    ENTRY = "entry"
    MID = "mid"
    EXIT = "exit"


@dataclass
class BalanceEvent:
    """A single balance observation at a point on track."""
    corner_name: str
    phase: Phase
    balance_type: BalanceType
    magnitude: float
    lap_dist_pct: float
    speed_kmh: float
    yaw_rate: float
    expected_yaw_rate: float


@dataclass
class CornerBaseline:
    """Learned baseline yaw rates for a corner from clean laps."""
    corner_name: str
    entry_yaw_rates: list[float] = field(default_factory=list)
    mid_yaw_rates: list[float] = field(default_factory=list)
    exit_yaw_rates: list[float] = field(default_factory=list)
    samples: int = 0

    def mean_yaw(self, phase: Phase) -> float:
        rates = {
            Phase.ENTRY: self.entry_yaw_rates,
            Phase.MID: self.mid_yaw_rates,
            Phase.EXIT: self.exit_yaw_rates,
        }[phase]
        if not rates:
            return 0.0
        return sum(rates) / len(rates)


class BalanceDetector:
    """Classify car balance using a bicycle-model comparison.

    Parameters:
        wheelbase: metres between axles (default 2.7 m, typical GT car)
        steering_ratio: steering wheel angle / road wheel angle (default 14:1)
        understeer_threshold: yaw-rate deficit fraction to flag understeer
        oversteer_threshold: yaw-rate excess fraction to flag oversteer
        slide_threshold: yaw-rate excess fraction to flag a full slide
    """

    def __init__(
        self,
        wheelbase: float = 2.7,
        steering_ratio: float = 14.0,
        understeer_threshold: float = 0.15,
        oversteer_threshold: float = 0.15,
        slide_threshold: float = 0.40,
    ) -> None:
        self.wheelbase = wheelbase
        self.steering_ratio = steering_ratio
        self.understeer_threshold = understeer_threshold
        self.oversteer_threshold = oversteer_threshold
        self.slide_threshold = slide_threshold
        self._baselines: dict[str, CornerBaseline] = {}

    def expected_yaw_rate(self, speed_ms: float, steer_rad: float) -> float:
        """Bicycle-model expected yaw rate in rad/s."""
        road_wheel = steer_rad / self.steering_ratio
        if abs(speed_ms) < 1.0:
            return 0.0
        return speed_ms * road_wheel / self.wheelbase

    def classify(
        self,
        speed_kmh: float,
        steer_rad: float,
        actual_yaw_rate: float,
        brake: float,
        throttle: float,
        corner: Optional[Corner] = None,
        lap_dist_pct: float = 0.0,
    ) -> Optional[BalanceEvent]:
        """Classify a single telemetry sample.

        Returns a :class:`BalanceEvent` when the sample shows a significant
        departure from neutral, ``None`` when the car is balanced or at
        too low a speed / steering input for the analysis to be meaningful.
        """
        speed_ms = speed_kmh / 3.6
        if speed_ms < 10.0 or abs(steer_rad) < 0.02:
            return None

        expected = self.expected_yaw_rate(speed_ms, steer_rad)
        if abs(expected) < 0.01:
            return None

        ratio = actual_yaw_rate / expected if expected != 0 else 0.0
        deficit = 1.0 - ratio

        if brake > 0.1:
            phase = Phase.ENTRY
        elif throttle > 0.5:
            phase = Phase.EXIT
        else:
            phase = Phase.MID

        if abs(deficit) > self.slide_threshold:
            balance_type = BalanceType.SLIDE
        elif deficit > self.understeer_threshold:
            balance_type = BalanceType.UNDERSTEER
        elif deficit < -self.oversteer_threshold:
            balance_type = BalanceType.OVERSTEER
        else:
            return None

        corner_name = corner.name if corner else f"pct_{lap_dist_pct:.3f}"

        return BalanceEvent(
            corner_name=corner_name,
            phase=phase,
            balance_type=balance_type,
            magnitude=abs(deficit),
            lap_dist_pct=lap_dist_pct,
            speed_kmh=speed_kmh,
            yaw_rate=actual_yaw_rate,
            expected_yaw_rate=expected,
        )

    def update_baseline(self, corner_name: str, phase: Phase,
                        yaw_rate: float) -> None:
        """Feed a clean-lap yaw rate sample into the per-corner baseline."""
        if corner_name not in self._baselines:
            self._baselines[corner_name] = CornerBaseline(corner_name=corner_name)
        bl = self._baselines[corner_name]
        bl.samples += 1
        max_history = 50
        target = {
            Phase.ENTRY: bl.entry_yaw_rates,
            Phase.MID: bl.mid_yaw_rates,
            Phase.EXIT: bl.exit_yaw_rates,
        }[phase]
        target.append(yaw_rate)
        if len(target) > max_history:
            target.pop(0)

    def get_baseline(self, corner_name: str) -> Optional[CornerBaseline]:
        return self._baselines.get(corner_name)
