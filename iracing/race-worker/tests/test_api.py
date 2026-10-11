"""Tests for the race-worker API."""

from __future__ import annotations

import time


def _create_session(client, headers):
    return client.post("/race/sessions", json={
        "sim": "iRacing",
        "track_id": "123",
        "track_name": "Spa-Francorchamps",
        "track_config": "Grand Prix",
        "car": "Mercedes W13",
        "series": "F1 Series",
        "session_type": "race",
        "race_length": "30 laps",
        "driver": "TestDriver",
    }, headers=headers)


class TestHealth:
    def test_health_no_auth_required(self, client):
        r = client.get("/race/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestAuth:
    def test_no_token_returns_401(self, client):
        r = client.post("/race/sessions", json={"sim": "iR", "track_id": "1", "track_name": "t", "car": "c"})
        assert r.status_code == 401

    def test_bad_token_returns_401(self, client):
        r = client.post("/race/sessions", json={"sim": "iR", "track_id": "1", "track_name": "t", "car": "c"},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_assistant_cannot_create_session(self, client, assistant_headers):
        r = client.post("/race/sessions", json={"sim": "iR", "track_id": "1", "track_name": "t", "car": "c"},
                        headers=assistant_headers)
        assert r.status_code == 403

    def test_app_cannot_create_message(self, client, app_headers):
        resp = _create_session(client, app_headers)
        sid = resp.json()["id"]
        r = client.post(f"/race/sessions/{sid}/messages",
                        json={"text": "hi", "priority": "normal", "expires_in_s": 60},
                        headers=app_headers)
        assert r.status_code == 403


class TestSessions:
    def test_create_and_get(self, client, app_headers, assistant_headers):
        r = _create_session(client, app_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["sim"] == "iRacing"
        assert data["active"] is True
        sid = data["id"]

        r2 = client.get(f"/race/sessions/{sid}", headers=assistant_headers)
        assert r2.status_code == 200
        assert r2.json()["id"] == sid

    def test_active_sessions(self, client, app_headers, assistant_headers):
        _create_session(client, app_headers)
        r = client.get("/race/sessions/active", headers=assistant_headers)
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_session_not_found(self, client, app_headers):
        r = client.get("/race/sessions/nonexistent", headers=app_headers)
        assert r.status_code == 404

    def test_end_session(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        r = client.post(f"/race/sessions/{sid}/end", headers=app_headers)
        assert r.status_code == 200
        assert r.json()["active"] is False
        assert r.json()["ended_at"] is not None


class TestLaps:
    def test_create_lap(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        r = client.post(f"/race/sessions/{sid}/laps", json={
            "lap": 1,
            "lap_time": 123.456,
            "position": 5,
            "class_position": 3,
            "gap_ahead": 1.2,
            "car_ahead_num": "44",
            "car_ahead_name": "Hamilton",
            "car_ahead_last_lap": 123.0,
            "gap_behind": 0.8,
            "car_behind_num": "33",
            "car_behind_name": "Verstappen",
            "car_behind_last_lap": 123.9,
            "incidents": 2,
            "incident_limit_dt": 17,
            "incident_limit_dq": 25,
            "fuel_level": 45.5,
            "fuel_per_lap": 2.3,
            "tyre_compound": "soft",
            "tyre_wear_fl": 85.0,
            "tyre_wear_fr": 82.0,
            "tyre_wear_rl": 90.0,
            "tyre_wear_rr": 88.0,
            "pit_this_lap": False,
            "off_track": True,
            "balance_notes": [
                {"corner": "T1", "phase": "entry", "type": "understeer", "magnitude": 6.0},
                {"corner": "T5", "phase": "exit", "type": "oversteer", "magnitude": 3.5},
            ],
        }, headers=app_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["lap"] == 1
        assert data["off_track"] is True
        assert len(data["balance_notes"]) == 2
        assert data["balance_notes"][0]["corner"] == "T1"

    def test_create_lap_session_not_found(self, client, app_headers):
        r = client.post("/race/sessions/nope/laps", json={"lap": 1}, headers=app_headers)
        assert r.status_code == 404


class TestEvents:
    def test_create_event(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        r = client.post(f"/race/sessions/{sid}/events", json={
            "event_type": "incident",
            "timestamp": "2024-06-01T12:00:00Z",
            "lap": 3,
            "lap_dist_pct": 0.45,
            "corner": "T5",
            "payload": {"contact_car": "44", "severity": "1x"},
        }, headers=app_headers)
        assert r.status_code == 201
        data = r.json()
        assert data["event_type"] == "incident"
        assert data["payload"]["severity"] == "1x"

    def test_invalid_event_type(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        r = client.post(f"/race/sessions/{sid}/events", json={
            "event_type": "invalid_type",
        }, headers=app_headers)
        assert r.status_code == 422


class TestState:
    def test_state_with_laps_and_events(self, client, app_headers, assistant_headers):
        sid = _create_session(client, app_headers).json()["id"]

        for i in range(1, 4):
            client.post(f"/race/sessions/{sid}/laps", json={
                "lap": i, "lap_time": 120.0 + i, "position": 5,
            }, headers=app_headers)

        client.post(f"/race/sessions/{sid}/events", json={
            "event_type": "incident", "lap": 2,
        }, headers=app_headers)

        r = client.get(f"/race/sessions/{sid}/state?n=2", headers=assistant_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["id"] == sid
        assert len(data["last_laps"]) == 2
        assert data["last_laps"][0]["lap"] == 3
        assert len(data["new_events"]) == 1
        assert data["latest_lap"]["lap"] == 3

    def test_state_since_cursor(self, client, app_headers, assistant_headers):
        sid = _create_session(client, app_headers).json()["id"]

        e1 = client.post(f"/race/sessions/{sid}/events", json={
            "event_type": "pit_in", "lap": 5,
        }, headers=app_headers).json()

        e2 = client.post(f"/race/sessions/{sid}/events", json={
            "event_type": "pit_out", "lap": 5,
        }, headers=app_headers).json()

        r = client.get(f"/race/sessions/{sid}/state?since={e1['id']}", headers=assistant_headers)
        data = r.json()
        assert len(data["new_events"]) == 1
        assert data["new_events"][0]["event_type"] == "pit_out"


class TestMessages:
    def test_message_lifecycle(self, client, app_headers, assistant_headers):
        sid = _create_session(client, app_headers).json()["id"]

        r = client.post(f"/race/sessions/{sid}/messages", json={
            "text": "Box this lap",
            "priority": "urgent",
            "expires_in_s": 120,
            "speak_when": "now",
        }, headers=assistant_headers)
        assert r.status_code == 201
        mid = r.json()["id"]

        r2 = client.get(f"/race/sessions/{sid}/messages/next", headers=app_headers)
        assert r2.status_code == 200
        msgs = r2.json()
        assert len(msgs) >= 1
        assert msgs[0]["text"] == "Box this lap"

        r3 = client.post(f"/race/sessions/{sid}/messages/{mid}/ack", headers=app_headers)
        assert r3.status_code == 204

        r4 = client.get(f"/race/sessions/{sid}/messages/next", headers=app_headers)
        assert len(r4.json()) == 0

    def test_message_priority_order(self, client, app_headers, assistant_headers):
        sid = _create_session(client, app_headers).json()["id"]

        client.post(f"/race/sessions/{sid}/messages", json={
            "text": "Low prio", "priority": "low", "expires_in_s": 120,
        }, headers=assistant_headers)
        client.post(f"/race/sessions/{sid}/messages", json={
            "text": "Urgent", "priority": "urgent", "expires_in_s": 120,
        }, headers=assistant_headers)
        client.post(f"/race/sessions/{sid}/messages", json={
            "text": "Normal", "priority": "normal", "expires_in_s": 120,
        }, headers=assistant_headers)

        msgs = client.get(f"/race/sessions/{sid}/messages/next", headers=app_headers).json()
        assert msgs[0]["priority"] == "urgent"
        assert msgs[1]["priority"] == "normal"
        assert msgs[2]["priority"] == "low"

    def test_ack_not_found(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        r = client.post(f"/race/sessions/{sid}/messages/99999/ack", headers=app_headers)
        assert r.status_code == 404


class TestExport:
    def test_export_full(self, client, app_headers, assistant_headers):
        sid = _create_session(client, app_headers).json()["id"]

        client.post(f"/race/sessions/{sid}/laps", json={"lap": 1, "lap_time": 120.5}, headers=app_headers)
        client.post(f"/race/sessions/{sid}/events", json={"event_type": "spin", "lap": 1}, headers=app_headers)
        client.post(f"/race/sessions/{sid}/messages", json={
            "text": "Careful T1", "expires_in_s": 60,
        }, headers=assistant_headers)
        client.post(f"/race/sessions/{sid}/end", headers=app_headers)

        r = client.get(f"/race/sessions/{sid}/export", headers=assistant_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["session"]["active"] is False
        assert len(data["laps"]) == 1
        assert len(data["events"]) == 1
        assert len(data["messages"]) == 1


class TestRateLimit:
    def test_rate_limit_not_triggered_normally(self, client, app_headers):
        sid = _create_session(client, app_headers).json()["id"]
        for i in range(5):
            r = client.post(f"/race/sessions/{sid}/laps",
                            json={"lap": i}, headers=app_headers)
            assert r.status_code == 201
