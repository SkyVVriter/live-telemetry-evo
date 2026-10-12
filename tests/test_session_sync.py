"""Tests for deferred race-worker session creation and YAML mapping."""
from __future__ import annotations

from unittest.mock import patch

from live_telemetry_evo.race_worker_client import (
    RaceWorkerClient,
    SessionSync,
    map_session_type,
    session_fingerprint,
    session_payload_from_iracing,
)
from live_telemetry_evo.sources.iracing import _parse_session_yaml
from live_telemetry_evo.telemetry import IRacingData


class TestMapSessionType:
    def test_qualify_variants(self):
        assert map_session_type("Lone Qualify") == "qualify"
        assert map_session_type("Open Qualify") == "qualify"

    def test_practice(self):
        assert map_session_type("Practice") == "practice"
        assert map_session_type("Open Practice") == "practice"

    def test_race_and_default(self):
        assert map_session_type("Race") == "race"
        assert map_session_type("") == "race"
        assert map_session_type("unknown") == "race"

    def test_warmup(self):
        assert map_session_type("Warmup") == "warmup"


class TestSessionPayload:
    def test_none_until_track_known(self):
        ir = IRacingData()
        assert session_payload_from_iracing(ir) is None

    def test_payload_from_yaml_fields(self):
        ir = IRacingData(
            track_id_iracing=274,
            track_name="Spa-Francorchamps",
            track_config="Grand Prix",
            car_name="Mercedes W13",
            series="F1",
            session_type="Lone Qualify",
            driver="TestDriver",
        )
        payload = session_payload_from_iracing(ir)
        assert payload == {
            "sim": "iRacing",
            "track_id": "274",
            "track_name": "Spa-Francorchamps",
            "track_config": "Grand Prix",
            "car": "Mercedes W13",
            "series": "F1",
            "session_type": "qualify",
            "driver": "TestDriver",
        }

    def test_fingerprint_changes_with_session_type(self):
        a = {"sim": "iRacing", "track_id": "1", "track_name": "t",
             "track_config": "", "car": "c", "series": "",
             "session_type": "practice", "driver": "d"}
        b = dict(a, session_type="race")
        assert session_fingerprint(a) != session_fingerprint(b)


class TestParseSessionYaml:
    def test_extracts_required_fields(self):
        ir = {
            "WeekendInfo": {
                "TrackID": 274,
                "TrackDisplayName": "Spa-Francorchamps",
                "TrackConfigName": "Grand Prix",
                "SeriesName": "IMSA",
            },
            "SessionInfo": {
                "Sessions": [
                    {"SessionNum": 0, "SessionType": "Practice",
                     "ResultsOfficialIncidentLimit": 17},
                    {"SessionNum": 1, "SessionType": "Race"},
                ],
            },
            "SessionNum": 1,
            "DriverInfo": {
                "DriverCarIdx": 2,
                "Drivers": [
                    {"CarIdx": 2, "UserName": "Sergey",
                     "CarNumber": "7", "CarScreenName": "BMW M4 GT3"},
                ],
            },
        }
        info = _parse_session_yaml(ir)
        assert info["track_id"] == 274
        assert info["track_name"] == "Spa-Francorchamps"
        assert info["track_config"] == "Grand Prix"
        assert info["series"] == "IMSA"
        assert info["session_type"] == "Race"
        assert info["session_num"] == 1
        assert info["car_name"] == "BMW M4 GT3"
        assert info["driver"] == "Sergey"
        assert info["incident_limit"] == 17


class TestSessionSync:
    def test_skips_until_payload_ready(self):
        client = RaceWorkerClient(base_url="https://host.com", token="tok")
        sync = SessionSync(client)
        with patch.object(client, "create_session") as create:
            assert sync.sync(None) is None
            create.assert_not_called()

    def test_creates_once_then_reuses(self):
        client = RaceWorkerClient(base_url="https://host.com", token="tok")
        sync = SessionSync(client)
        payload = session_payload_from_iracing(IRacingData(
            track_id_iracing=1, track_name="Monza", car_name="Ferrari",
            session_type="Race",
        ))
        with patch.object(client, "create_session", return_value="abc") as create:
            assert sync.sync(payload) == "abc"
            client._session_id = "abc"
            assert sync.sync(payload) == "abc"
            create.assert_called_once()

    def test_recreates_on_session_change(self):
        client = RaceWorkerClient(base_url="https://host.com", token="tok")
        sync = SessionSync(client)
        first = session_payload_from_iracing(IRacingData(
            track_id_iracing=1, track_name="Monza", car_name="Ferrari",
            session_type="Practice",
        ))
        second = session_payload_from_iracing(IRacingData(
            track_id_iracing=1, track_name="Monza", car_name="Ferrari",
            session_type="Race",
        ))
        with patch.object(client, "create_session",
                          side_effect=["s1", "s2"]) as create, \
             patch.object(client, "end_session") as end:
            assert sync.sync(first) == "s1"
            client._session_id = "s1"
            assert sync.sync(second) == "s2"
            end.assert_called_once()
            assert create.call_count == 2


class TestCreateSession422:
    def test_logs_422_body_and_does_not_retry(self):
        import urllib.error

        client = RaceWorkerClient(base_url="https://host.com", token="tok")
        err = urllib.error.HTTPError(
            "https://host.com/race/sessions", 422, "Unprocessable Entity",
            hdrs={}, fp=None)

        with patch("live_telemetry_evo.race_worker_client._request",
                   side_effect=err) as req, \
             patch("live_telemetry_evo.race_worker_client._http_error_body",
                   return_value='{"detail":"Field required"}'), \
             patch("live_telemetry_evo.race_worker_client.log") as log_fn, \
             patch("time.sleep") as sleep:
            assert client.create_session(source="iracing") is None
            assert req.call_count == 1
            sleep.assert_not_called()
            logged = " ".join(str(c) for c in log_fn.call_args_list)
            assert "422" in logged
            assert "Field required" in logged
