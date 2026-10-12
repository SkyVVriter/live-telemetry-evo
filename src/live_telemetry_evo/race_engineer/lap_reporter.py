"""Post completed laps to race-worker, including the last lap of a session.

Qualify → race (Motegi) used to drop the final flying lap: SessionSync
ended the session before ``Lap`` incremented, and out-laps were stored as
``lap_time`` 0.0. This reporter posts on lap increment, on
``LapCompleted`` increment, when ``SessionState`` goes checkered/cooldown,
and when the session fingerprint changes — using ``LapLastLapTime``.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..telemetry import TelemetryFrame
from .lap_summary import LapSummary, LapSummaryBuilder

# iRacing SessionState
SESSION_INVALID = 0
SESSION_GET_IN_CAR = 1
SESSION_WARMUP = 2
SESSION_PARADE = 3
SESSION_RACING = 4
SESSION_CHECKERED = 5
SESSION_COOL_DOWN = 6
TERMINAL_STATES = frozenset({SESSION_CHECKERED, SESSION_COOL_DOWN})

_TIME_EPS = 0.002


def sane_position(value: int | float | None) -> int:
    """PlayerCarPosition is -1 / 0 when the car is not on the timing sheet."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def lap_to_api(summary: LapSummary) -> dict:
    """Map a lap summary onto race-worker ``LapCreate`` field names."""
    out_lap = bool(summary.out_lap or summary.lap_time <= 0.0)
    return {
        "lap": summary.lap_number,
        "lap_time": None if out_lap else round(summary.lap_time, 3),
        "out_lap": out_lap,
        "position": summary.position if summary.position > 0 else None,
        "class_position": (
            summary.class_position if summary.class_position > 0 else None
        ),
        "incidents": summary.incidents_this_lap,
        "fuel_level": round(summary.fuel_remaining, 3),
        "fuel_per_lap": round(summary.fuel_used, 3),
        "pit_this_lap": summary.pit_this_lap,
        "off_track": summary.off_track,
        "balance_notes": summary.balance_notes_for_api(),
    }


class LapReporter:
    """Finish and POST laps; flush the last completed lap before session end."""

    def __init__(
        self,
        builder: LapSummaryBuilder,
        post_lap: Optional[Callable[[dict], bool]] = None,
    ) -> None:
        self.builder = builder
        self._post_lap = post_lap
        self._prev_lap = 0
        self._prev_completed = -1
        self._posted_laps: set[int] = set()
        self._posted_times: list[float] = []
        self._session_state = SESSION_INVALID
        self._seen_running = False
        self._last_lap_time_s = 0.0
        self._last_laps_completed = 0

    def set_post_lap(self, post_lap: Optional[Callable[[dict], bool]]) -> None:
        self._post_lap = post_lap

    def reset_session(self) -> None:
        """Clear per-session bookkeeping after a new race-worker session."""
        self._prev_lap = 0
        self._prev_completed = -1
        self._posted_laps.clear()
        self._posted_times.clear()
        self._session_state = SESSION_INVALID
        self._seen_running = False
        self._last_lap_time_s = 0.0
        self._last_laps_completed = 0
        self.builder.new_lap(0, fuel=0.0, incidents=0)

    def note_official_lap(self, last_lap_time_s: float,
                          laps_completed: int = 0) -> None:
        """Remember official iRacing values from the current frame.

        Called *before* SessionSync so a qualify→race transition frame
        can still flush ``LapLastLapTime`` / ``LapCompleted``.
        """
        if last_lap_time_s > 0:
            self._last_lap_time_s = last_lap_time_s
        if laps_completed > 0:
            self._last_laps_completed = laps_completed

    def flush_pending(self) -> Optional[dict]:
        """SessionSync ``on_before_end`` hook — post the last completed lap."""
        return self.flush_final(self._last_lap_time_s)

    def flush_final(self, last_lap_time_s: float = 0.0) -> Optional[dict]:
        """Post the in-progress lap using the official last-lap time.

        If ``last_lap_time_s`` was already posted (previous flying lap),
        the current lap is treated as incomplete / out-lap.
        """
        current = self.builder.current_summary()
        if current.lap_number <= 0 or current.lap_number in self._posted_laps:
            return None
        if not current.was_driven():
            return None
        use_time = last_lap_time_s if last_lap_time_s > 0 else self._last_lap_time_s
        if use_time > 0 and self._time_already_posted(use_time):
            use_time = 0.0
        return self._finish_and_post(use_time)

    def on_frame(self, frame: TelemetryFrame) -> Optional[dict]:
        ir = frame.iracing
        eng = frame.engine
        inp = frame.inputs
        last_s = eng.last_lap_time_ms / 1000.0 if eng.last_lap_time_ms > 0 else 0.0
        self.note_official_lap(last_s, ir.laps_completed)

        posted: Optional[dict] = None
        if ir.session_state in TERMINAL_STATES:
            if self._seen_running and self._session_state not in TERMINAL_STATES:
                # Leave the finished lap in place so a later session-end
                # flush sees it already posted and does not emit a ghost out-lap.
                posted = self.flush_final(last_s) or posted
        else:
            self._seen_running = True
        self._session_state = ir.session_state

        cur_lap = eng.current_lap
        completed = ir.laps_completed
        pos = sane_position(ir.player_position)
        cpos = sane_position(ir.player_class_position)

        if self._prev_lap == 0:
            self.builder.new_lap(
                cur_lap or 1, fuel=eng.fuel_liters,
                incidents=ir.incident_count, position=pos, class_position=cpos)
        elif cur_lap > self._prev_lap:
            posted = self._finish_and_post(last_s) or posted
            self.builder.new_lap(
                cur_lap, fuel=eng.fuel_liters,
                incidents=ir.incident_count, position=pos, class_position=cpos)
        elif (
            completed > self._prev_completed
            and self._prev_completed >= 0
            and self.builder.current_summary().lap_number not in self._posted_laps
        ):
            posted = self._finish_and_post(last_s) or posted
            nxt = max(cur_lap, self.builder.current_summary().lap_number + 1)
            self.builder.new_lap(
                nxt, fuel=eng.fuel_liters,
                incidents=ir.incident_count, position=pos, class_position=cpos)

        self._prev_lap = cur_lap
        self._prev_completed = completed

        self.builder.add_sample(
            speed_kmh=eng.speed_kmh,
            steer_rad=ir.steering_angle_rad,
            yaw_rate=ir.yaw_rate,
            brake=inp.brake,
            throttle=inp.throttle,
            lap_dist_pct=ir.lap_dist_pct,
            fuel=eng.fuel_liters,
            incidents=ir.incident_count,
            on_pit_road=ir.on_pit_road,
            position=pos,
            class_position=cpos,
        )
        return posted

    def _time_already_posted(self, lap_time: float) -> bool:
        return any(abs(t - lap_time) < _TIME_EPS for t in self._posted_times)

    def _finish_and_post(self, lap_time: float,
                         out_lap: bool | None = None) -> Optional[dict]:
        current = self.builder.current_summary()
        if current.lap_number <= 0 or current.lap_number in self._posted_laps:
            return None
        if not current.was_driven():
            return None
        summary = self.builder.finish_lap(lap_time=lap_time, out_lap=out_lap)
        payload = lap_to_api(summary)
        if self._post_lap is not None:
            self._post_lap(payload)
        self._posted_laps.add(summary.lap_number)
        if summary.lap_time > 0 and not summary.out_lap:
            self._posted_times.append(summary.lap_time)
        return payload
