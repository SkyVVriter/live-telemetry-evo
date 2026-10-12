"""Motegi qualify data bugs: last-lap flush, fuel/position, balance notes."""
from __future__ import annotations

from live_telemetry_evo.race_engineer.balance import (
    BalanceDetector,
    BalanceEvent,
    BalanceType,
    Phase,
)
from live_telemetry_evo.race_engineer.incidents import IncidentTracker
from live_telemetry_evo.race_engineer.lap_reporter import (
    SESSION_CHECKERED,
    SESSION_COOL_DOWN,
    SESSION_RACING,
    LapReporter,
    lap_to_api,
    sane_position,
)
from live_telemetry_evo.race_engineer.lap_summary import (
    LapSummary,
    LapSummaryBuilder,
    aggregate_balance_notes,
)
from live_telemetry_evo.sources.iracing import _sdk_position
from live_telemetry_evo.telemetry import TelemetryFrame


def _builder() -> LapSummaryBuilder:
    return LapSummaryBuilder(BalanceDetector(), IncidentTracker())


def _frame(
    *,
    lap: int = 1,
    last_s: float = 0.0,
    fuel: float = 50.0,
    pos: int = 1,
    class_pos: int = 1,
    session_state: int = SESSION_RACING,
    laps_completed: int = 0,
    speed: float = 180.0,
    pct: float = 0.4,
    incidents: int = 0,
    pit: bool = False,
) -> TelemetryFrame:
    f = TelemetryFrame()
    f.engine.current_lap = lap
    f.engine.last_lap_time_ms = int(round(last_s * 1000)) if last_s > 0 else 0
    f.engine.fuel_liters = fuel
    f.engine.speed_kmh = speed
    f.inputs.throttle = 0.6
    f.inputs.brake = 0.0
    f.iracing.player_position = pos
    f.iracing.player_class_position = class_pos
    f.iracing.session_state = session_state
    f.iracing.laps_completed = laps_completed
    f.iracing.lap_dist_pct = pct
    f.iracing.incident_count = incidents
    f.iracing.on_pit_road = pit
    f.iracing.steering_angle_rad = 0.05
    f.iracing.yaw_rate = 0.05
    return f


def _drive(reporter: LapReporter, **kwargs) -> None:
    """A few samples so the current lap counts as driven."""
    pct = float(kwargs.pop("pct", 0.2))
    for i in range(4):
        reporter.on_frame(_frame(pct=pct + i * 0.01, **kwargs))


class TestSanePosition:
    def test_negative_and_zero_are_unset(self):
        assert sane_position(-1) == 0
        assert sane_position(0) == 0
        assert _sdk_position(-1) == 0
        assert _sdk_position(None) == 0

    def test_positive_kept(self):
        assert sane_position(7) == 7
        assert _sdk_position(3) == 3


class TestLapToApi:
    def test_maps_fuel_and_hides_invalid_position(self):
        s = LapSummary(
            lap_number=2,
            lap_time=114.366,
            position=-1,
            class_position=0,
            fuel_used=2.4,
            fuel_remaining=47.6,
            out_lap=False,
        )
        payload = lap_to_api(s)
        assert "fuel_used" not in payload
        assert "fuel_remaining" not in payload
        assert payload["fuel_level"] == 47.6
        assert payload["fuel_per_lap"] == 2.4
        assert payload["position"] is None
        assert payload["class_position"] is None
        assert payload["lap_time"] == 114.366
        assert payload["out_lap"] is False

    def test_out_lap_does_not_post_zero_time(self):
        s = LapSummary(lap_number=1, lap_time=0.0, out_lap=True,
                       fuel_remaining=80.0, fuel_used=0.4)
        payload = lap_to_api(s)
        assert payload["out_lap"] is True
        assert payload["lap_time"] is None
        assert payload["lap_time"] != 0.0


class TestMotegiLastLapFlush:
    """(a) Last qualify lap 1:53.109 must be posted before session end."""

    def test_flush_on_qualify_to_race_transition(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)

        # Out-lap 1, then flying lap 2 (114.366), then flying lap 3.
        _drive(reporter, lap=1, last_s=0.0, fuel=80.0, pos=12, laps_completed=0)
        _drive(reporter, lap=2, last_s=0.0, fuel=78.5, pos=12, laps_completed=1)
        _drive(reporter, lap=3, last_s=114.366, fuel=76.1, pos=11,
               laps_completed=2, pct=0.5)
        # Completed 1:53.109 but Lap never increments — session switches.
        reporter.note_official_lap(113.109, laps_completed=3)
        flushed = reporter.flush_pending()

        laps = {p["lap"]: p for p in posted}
        assert 1 in laps and laps[1]["out_lap"] is True
        assert laps[1]["lap_time"] is None
        assert laps[2]["lap_time"] == 114.366
        assert laps[2]["out_lap"] is False
        assert flushed is not None
        assert flushed["lap"] == 3
        assert flushed["lap_time"] == 113.109
        assert flushed["out_lap"] is False
        assert len(posted) == 3

    def test_flush_on_checkered(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        _drive(reporter, lap=1, last_s=0.0, fuel=60.0, laps_completed=0)
        _drive(reporter, lap=2, last_s=0.0, fuel=58.0, laps_completed=1)
        _drive(reporter, lap=3, last_s=114.366, fuel=55.5, laps_completed=2)
        reporter.on_frame(_frame(
            lap=3, last_s=113.109, fuel=53.2, laps_completed=3,
            session_state=SESSION_CHECKERED, speed=200.0, pct=0.99,
        ))
        laps = {p["lap"]: p for p in posted}
        assert laps[3]["lap_time"] == 113.109
        assert laps[3]["out_lap"] is False

    def test_flush_on_cooldown(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        _drive(reporter, lap=2, last_s=90.0, fuel=40.0, laps_completed=1)
        reporter.on_frame(_frame(
            lap=2, last_s=91.5, fuel=38.0, laps_completed=2,
            session_state=SESSION_COOL_DOWN, speed=120.0,
        ))
        assert any(p["lap"] == 2 and p["lap_time"] == 91.5 for p in posted)

    def test_transition_does_not_double_post_after_checkered(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        _drive(reporter, lap=2, last_s=100.0, fuel=40.0, laps_completed=1)
        reporter.on_frame(_frame(
            lap=2, last_s=101.2, fuel=38.0, laps_completed=2,
            session_state=SESSION_CHECKERED, speed=150.0,
        ))
        n = len(posted)
        reporter.flush_pending()
        assert len(posted) == n

    def test_stale_last_time_does_not_reuse_previous_lap(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        _drive(reporter, lap=1, last_s=0.0, fuel=50.0)
        _drive(reporter, lap=2, last_s=0.0, fuel=48.0)
        _drive(reporter, lap=3, last_s=114.366, fuel=46.0, laps_completed=2)
        # Session ends mid-lap 3; last official time is still lap 2.
        reporter.note_official_lap(114.366, laps_completed=2)
        flushed = reporter.flush_pending()
        assert flushed is not None
        assert flushed["lap"] == 3
        assert flushed["out_lap"] is True
        assert flushed["lap_time"] is None


class TestFuelAndPosition:
    """(b) fuel_level / fuel_per_lap / position from telemetry."""

    def test_fuel_delta_and_live_position(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        reporter.on_frame(_frame(lap=1, fuel=50.0, pos=-1, class_pos=0, speed=160))
        reporter.on_frame(_frame(lap=1, fuel=49.2, pos=-1, class_pos=0, speed=170, pct=0.4))
        reporter.on_frame(_frame(lap=1, fuel=48.0, pos=5, class_pos=3, speed=180, pct=0.8))
        reporter.on_frame(_frame(lap=2, last_s=90.5, fuel=47.5, pos=5, class_pos=3,
                                 laps_completed=1, speed=100, pct=0.02))
        assert len(posted) == 1
        p = posted[0]
        assert p["fuel_level"] == 48.0
        assert p["fuel_per_lap"] == 2.0
        assert p["position"] == 5
        assert p["class_position"] == 3
        assert p["lap_time"] == 90.5

    def test_invalid_position_is_null_not_minus_one(self):
        posted: list[dict] = []
        reporter = LapReporter(_builder(), post_lap=posted.append)
        _drive(reporter, lap=1, fuel=30.0, pos=-1, class_pos=0)
        _drive(reporter, lap=2, last_s=88.0, fuel=28.5, pos=-1, class_pos=0,
               laps_completed=1)
        assert posted[0]["position"] is None
        assert posted[0]["class_position"] is None
        assert posted[0]["fuel_level"] == 28.5 or posted[0]["fuel_level"] > 0
        assert posted[0]["fuel_per_lap"] >= 0


class TestBalanceNoteAggregation:
    """(c) ~700–1000 pct_ samples must collapse to corner-level notes."""

    def _ev(self, pct: float, *, phase=Phase.MID, btype=BalanceType.UNDERSTEER,
            mag=0.3, name: str | None = None) -> BalanceEvent:
        return BalanceEvent(
            corner_name=name or f"pct_{pct:.3f}",
            phase=phase,
            balance_type=btype,
            magnitude=mag,
            lap_dist_pct=pct,
            speed_kmh=120.0,
            yaw_rate=0.2,
            expected_yaw_rate=0.4,
        )

    def test_collapses_hundreds_of_pct_samples(self):
        events = []
        # ~800 samples across two corners, 0.001 pct steps, several per step
        for i in range(80):
            pct = 0.100 + i * 0.001
            for _ in range(5):
                events.append(self._ev(pct, phase=Phase.ENTRY, mag=0.22))
        for i in range(80):
            pct = 0.400 + i * 0.001
            for _ in range(5):
                events.append(self._ev(
                    pct, phase=Phase.EXIT, btype=BalanceType.OVERSTEER, mag=0.41))
        assert len(events) == 800
        notes = aggregate_balance_notes(events)
        assert len(notes) <= 4
        assert len(notes) >= 2
        corners = {n["corner"] for n in notes}
        assert corners == {"T1", "T2"}
        by_phase = {n["phase"]: n for n in notes}
        assert by_phase["entry"]["type"] == "understeer"
        assert by_phase["exit"]["type"] == "oversteer"
        assert by_phase["exit"]["magnitude"] == 4.1

    def test_peak_magnitude_and_named_corner(self):
        events = [
            self._ev(0.12, phase=Phase.ENTRY, mag=0.20, name="T1"),
            self._ev(0.13, phase=Phase.ENTRY, mag=0.55, name="T1"),
            self._ev(0.14, phase=Phase.MID, mag=0.18, name="T1"),
        ]
        notes = aggregate_balance_notes(events)
        assert len(notes) == 2
        entry = next(n for n in notes if n["phase"] == "entry")
        assert entry["corner"] == "T1"
        assert entry["magnitude"] == 5.5

    def test_drops_isolated_pct_sample(self):
        notes = aggregate_balance_notes([self._ev(0.77)])
        assert notes == []

    def test_summary_uses_aggregator(self):
        events = [self._ev(0.20 + i * 0.001, mag=0.3) for i in range(12)]
        s = LapSummary(lap_number=1, balance_events=events)
        notes = s.balance_notes_for_api()
        assert len(notes) == 1
        assert notes[0]["corner"] == "T1"
