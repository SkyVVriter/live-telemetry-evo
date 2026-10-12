"""Intent handling for driver voice commands.

Simple local commands are answered by the TTS engine directly;
everything else is forwarded to the race-worker as a ``driver_feedback``
event so the AI assistant sees it in ``/state``.

Local commands:
- "статус" / "status" -> current position, gap, incidents summary
- "гапы" / "gaps" -> gaps ahead/behind
- "топливо" / "fuel" -> fuel remaining, fuel per lap
- "инки" / "incidents" -> incident count and limits
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..logbook import log


@dataclass
class DriverContext:
    """Snapshot of current race state for intent responses."""
    position: int = 0
    class_position: int = 0
    lap: int = 0
    lap_dist_pct: float = 0.0
    corner_name: str = ""
    incidents: int = 0
    incident_limit_dt: int = 0
    incident_limit_dq: int = 0
    fuel_remaining: float = 0.0
    fuel_per_lap: float = 0.0
    gap_ahead: float = 0.0
    gap_behind: float = 0.0
    car_ahead_name: str = ""
    car_behind_name: str = ""
    laps_remaining: int = 0
    speed_kmh: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0
    steering_rad: float = 0.0
    yaw_rate: float = 0.0


_LOCAL_COMMANDS: dict[str, list[str]] = {
    "status": ["статус", "status", "стейтус"],
    "gaps": ["гапы", "gaps", "гэпы", "разрывы", "интервалы"],
    "fuel": ["топливо", "fuel", "бензин"],
    "incidents": ["инки", "incidents", "инциденты", "инцидент"],
}


def _match_intent(text: str) -> Optional[str]:
    """Match transcribed text to a local command intent."""
    normalized = text.lower().strip()
    normalized = re.sub(r"[.,!?\-—]", "", normalized).strip()
    words = normalized.split()
    if not words:
        return None
    for intent, triggers in _LOCAL_COMMANDS.items():
        for trigger in triggers:
            if trigger in words or normalized == trigger:
                return intent
    return None


def _build_status_response(ctx: DriverContext) -> str:
    parts = []
    if ctx.position > 0:
        parts.append(f"Позиция {ctx.position}")
    if ctx.class_position > 0 and ctx.class_position != ctx.position:
        parts.append(f"в классе {ctx.class_position}")
    parts.append(f"круг {ctx.lap}")
    if ctx.incidents > 0:
        parts.append(f"инцидентов {ctx.incidents}")
    if ctx.fuel_remaining > 0:
        parts.append(f"топлива {ctx.fuel_remaining:.1f} литров")
    return ", ".join(parts) + "."


def _build_gaps_response(ctx: DriverContext) -> str:
    parts = []
    if ctx.gap_ahead > 0:
        ahead = ctx.car_ahead_name or "впереди"
        parts.append(f"До {ahead}: {ctx.gap_ahead:.1f} секунд")
    else:
        parts.append("Впереди никого нет")
    if ctx.gap_behind > 0:
        behind = ctx.car_behind_name or "позади"
        parts.append(f"от {behind}: {ctx.gap_behind:.1f} секунд")
    else:
        parts.append("Позади чисто")
    return ". ".join(parts) + "."


def _build_fuel_response(ctx: DriverContext) -> str:
    parts = [f"Топлива {ctx.fuel_remaining:.1f} литров"]
    if ctx.fuel_per_lap > 0:
        laps_left = ctx.fuel_remaining / ctx.fuel_per_lap if ctx.fuel_per_lap > 0 else 0
        parts.append(f"расход {ctx.fuel_per_lap:.1f} на круг")
        parts.append(f"хватит на {laps_left:.0f} кругов")
    return ", ".join(parts) + "."


def _build_incidents_response(ctx: DriverContext) -> str:
    parts = [f"Инцидентов {ctx.incidents}"]
    if ctx.incident_limit_dt > 0:
        remaining = ctx.incident_limit_dt - ctx.incidents
        parts.append(f"до DT осталось {remaining}")
    if ctx.incident_limit_dq > 0:
        remaining = ctx.incident_limit_dq - ctx.incidents
        parts.append(f"до DQ осталось {remaining}")
    return ", ".join(parts) + "."


_RESPONSE_BUILDERS = {
    "status": _build_status_response,
    "gaps": _build_gaps_response,
    "fuel": _build_fuel_response,
    "incidents": _build_incidents_response,
}


class IntentHandler:
    """Dispatch transcribed text to local commands or remote events.

    Parameters:
        on_speak: Callback to speak a response via TTS.
        on_driver_feedback: Callback to send a driver_feedback event to
            race-worker with (transcript, audio_duration, context).
        get_context: Callable returning the current DriverContext.
    """

    def __init__(
        self,
        on_speak: Callable[[str], None] | None = None,
        on_driver_feedback: Callable[[str, float, DriverContext], None] | None = None,
        get_context: Callable[[], DriverContext] | None = None,
    ) -> None:
        self.on_speak = on_speak
        self.on_driver_feedback = on_driver_feedback
        self.get_context = get_context

    def handle(self, text: str, audio_duration: float = 0.0) -> str:
        """Process transcribed text. Returns the intent name or 'feedback'.

        Every transcript is sent to race-worker as a ``driver_feedback``
        event (with telemetry snapshot) so it is retrievable via
        ``/race/state?since=``. Local commands additionally produce a
        spoken TTS response.
        """
        if not text.strip():
            return "empty"

        intent = _match_intent(text)
        ctx = self.get_context() if self.get_context else DriverContext()

        if intent is not None:
            builder = _RESPONSE_BUILDERS.get(intent)
            if builder:
                response = builder(ctx)
                log(f"[ptt/intent] local command '{intent}': {response}")
                if self.on_speak:
                    self.on_speak(response)

        if self.on_driver_feedback:
            self.on_driver_feedback(text, audio_duration, ctx)

        if intent is not None:
            return intent
        log(f"[ptt/intent] driver feedback: {text[:80]}")
        return "feedback"
