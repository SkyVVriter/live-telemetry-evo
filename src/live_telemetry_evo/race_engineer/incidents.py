"""Incident tracker with configurable thresholds.

Monitors the cumulative incident count from iRacing and generates
warnings as configurable thresholds are approached or exceeded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class IncidentSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class IncidentAlert:
    severity: IncidentSeverity
    message: str
    current_count: int
    limit: int
    label: str


@dataclass
class IncidentThresholds:
    """Configurable incident thresholds. When *_limit is 0, that level
    is disabled (auto-detected from session info when available)."""
    warn_at: int = 20
    dt_limit: int = 25
    dq_limit: int = 32


class IncidentTracker:
    """Tracks incident count and generates alerts at thresholds."""

    def __init__(self, thresholds: Optional[IncidentThresholds] = None) -> None:
        self.thresholds = thresholds or IncidentThresholds()
        self._last_count = 0
        self._warned_levels: set[str] = set()

    def set_limits_from_session(self, dt_limit: int, dq_limit: int) -> None:
        if dt_limit > 0:
            self.thresholds.dt_limit = dt_limit
        if dq_limit > 0:
            self.thresholds.dq_limit = dq_limit
        if dt_limit > 0 and self.thresholds.warn_at >= dt_limit:
            self.thresholds.warn_at = max(1, dt_limit - 5)

    def update(self, incident_count: int) -> list[IncidentAlert]:
        """Process a new cumulative incident count. Returns any new alerts."""
        alerts: list[IncidentAlert] = []
        th = self.thresholds

        if incident_count > self._last_count:
            new_inc = incident_count - self._last_count
            alerts.append(IncidentAlert(
                severity=IncidentSeverity.INFO,
                message=f"+{new_inc}x инцидент (всего {incident_count}x)",
                current_count=incident_count,
                limit=th.dq_limit,
                label="incident",
            ))

        if th.dq_limit > 0 and incident_count >= th.dq_limit:
            if "dq" not in self._warned_levels:
                self._warned_levels.add("dq")
                alerts.append(IncidentAlert(
                    severity=IncidentSeverity.CRITICAL,
                    message=f"DQ! Инциденты: {incident_count}x / {th.dq_limit}x",
                    current_count=incident_count,
                    limit=th.dq_limit,
                    label="dq",
                ))
        elif th.dt_limit > 0 and incident_count >= th.dt_limit:
            if "dt" not in self._warned_levels:
                self._warned_levels.add("dt")
                alerts.append(IncidentAlert(
                    severity=IncidentSeverity.CRITICAL,
                    message=f"Drive-Through! Инциденты: {incident_count}x / {th.dt_limit}x",
                    current_count=incident_count,
                    limit=th.dt_limit,
                    label="dt",
                ))
        elif th.warn_at > 0 and incident_count >= th.warn_at:
            if "warn" not in self._warned_levels:
                self._warned_levels.add("warn")
                remaining = th.dt_limit - incident_count if th.dt_limit > 0 else 0
                alerts.append(IncidentAlert(
                    severity=IncidentSeverity.WARNING,
                    message=f"Внимание: {incident_count}x инцидентов"
                            + (f" (до DT осталось {remaining}x)" if remaining > 0 else ""),
                    current_count=incident_count,
                    limit=th.dt_limit,
                    label="warn",
                ))

        self._last_count = incident_count
        return alerts

    def reset(self) -> None:
        self._last_count = 0
        self._warned_levels.clear()
