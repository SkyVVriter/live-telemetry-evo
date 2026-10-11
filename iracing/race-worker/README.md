# race-worker

A lightweight Python relay service between an iRacing voice-engineer app running on the driver's PC and a remote AI assistant (RaceBot) that polls it during races.

The app on the PC pushes telemetry (laps, events) to the service. RaceBot polls for state updates and queues short TTS messages back. The PC app picks up those messages and speaks them to the driver.

## Architecture

```
  Driver PC (iRacing)          race-worker (VPS)           RaceBot (LLM)
  ┌──────────────┐            ┌───────────────┐           ┌──────────┐
  │ voice-eng app│──POST───▶  │ FastAPI :8091  │  ◀──GET── │ poller   │
  │  laps/events │            │ SQLite (WAL)   │           │ messages │
  │  GET messages│◀──────────│               │──────────▶│ state    │
  └──────────────┘            └───────────────┘           └──────────┘
                                     ▲
                               Caddy (TLS)
                          acevo-ai.duckdns.org
```

## Stack

- Python 3.12, FastAPI, Uvicorn
- SQLite with WAL mode
- Bearer-token auth (two roles: `APP_TOKEN` for the PC app, `ASSISTANT_TOKEN` for RaceBot)
- Basic rate limiting on ingest endpoints
- Target: Ubuntu 24.04, systemd, Caddy reverse proxy

## Quick Start (Development)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export APP_TOKEN=dev-app-token
export ASSISTANT_TOKEN=dev-assistant-token
export RACE_WORKER_DB=./data/race.db

uvicorn app.main:app --host 127.0.0.1 --port 8091 --reload
```

Run tests:

```bash
python3 -m pytest tests/ -v
```

Run the mock client (requires a running server):

```bash
python3 scripts/mock_app.py \
    --base-url http://127.0.0.1:8091 \
    --app-token dev-app-token \
    --assistant-token dev-assistant-token \
    --laps 10
```

## Deployment

See the `deploy/` directory:

| File | Purpose |
|------|---------|
| `install.sh` | Idempotent setup script (run as root) |
| `race-worker.service` | Hardened systemd unit |
| `env.example` | Template for `/etc/race-worker/env` |
| `Caddyfile.snippet` | `handle /race/*` block for Caddy |

```bash
cd /path/to/race-worker
sudo bash deploy/install.sh
sudo nano /etc/race-worker/env          # set real tokens
# add Caddyfile snippet, then: sudo systemctl reload caddy
sudo systemctl enable --now race-worker
```

## API Reference

All endpoints are under `/race`. Auth is via `Authorization: Bearer <token>` header.

**Roles:** `APP` = PC app token, `ASST` = assistant token, `ANY` = either token.

### Health

```
GET /race/health                        (no auth)
```

```bash
curl http://127.0.0.1:8091/race/health
# {"status":"ok","version":"0.1.0"}
```

### Sessions

```
POST /race/sessions                     (APP)
GET  /race/sessions/active              (ANY)
GET  /race/sessions/{id}                (ANY)
POST /race/sessions/{id}/end            (APP)
GET  /race/sessions/{id}/export         (ANY)
```

**Create session:**

```bash
curl -X POST http://127.0.0.1:8091/race/sessions \
  -H "Authorization: Bearer $APP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sim": "iRacing",
    "track_id": "274",
    "track_name": "Spa-Francorchamps",
    "track_config": "Grand Prix",
    "car": "Dallara P217 LMP2",
    "series": "IMSA Endurance",
    "session_type": "race",
    "race_length": "30 laps",
    "driver": "DriverName"
  }'
```

**Get active sessions:**

```bash
curl http://127.0.0.1:8091/race/sessions/active \
  -H "Authorization: Bearer $APP_TOKEN"
```

### Laps

```
POST /race/sessions/{id}/laps           (APP)
```

```bash
curl -X POST http://127.0.0.1:8091/race/sessions/$SID/laps \
  -H "Authorization: Bearer $APP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "lap": 5,
    "lap_time": 128.432,
    "position": 3,
    "class_position": 1,
    "gap_ahead": 1.234,
    "car_ahead_num": "44",
    "car_ahead_name": "Hamilton",
    "car_ahead_last_lap": 128.100,
    "gap_behind": 0.876,
    "car_behind_num": "33",
    "car_behind_name": "Verstappen",
    "car_behind_last_lap": 129.001,
    "incidents": 4,
    "incident_limit_dt": 17,
    "incident_limit_dq": 25,
    "fuel_level": 45.2,
    "fuel_per_lap": 2.1,
    "tyre_compound": "medium",
    "tyre_wear_fl": 85.0,
    "tyre_wear_fr": 82.5,
    "tyre_wear_rl": 90.0,
    "tyre_wear_rr": 88.5,
    "pit_this_lap": false,
    "off_track": false,
    "balance_notes": [
      {"corner": "T1", "phase": "entry", "type": "understeer", "magnitude": 6.0}
    ]
  }'
```

### Events

```
POST /race/sessions/{id}/events         (APP)
```

Event types: `incident`, `slide`, `spin`, `plow`, `overtake`, `overtaken`, `pit_in`, `pit_out`, `flag_black`, `flag_blue`, `flag_dq`.

```bash
curl -X POST http://127.0.0.1:8091/race/sessions/$SID/events \
  -H "Authorization: Bearer $APP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "incident",
    "timestamp": "2024-06-01T12:34:56Z",
    "lap": 5,
    "lap_dist_pct": 0.45,
    "corner": "T5",
    "payload": {"contact_car": "44", "severity": "2x"}
  }'
```

### State (assistant polling)

```
GET /race/sessions/{id}/state           (ANY)
    ?since=<event_id>    only events after this id
    &n=5                 number of recent laps (1-50)
```

```bash
curl "http://127.0.0.1:8091/race/sessions/$SID/state?since=42&n=3" \
  -H "Authorization: Bearer $ASSISTANT_TOKEN"
```

Returns: `{ session, latest_lap, last_laps[], new_events[], pending_messages }`.

### Messages (TTS relay)

```
POST /race/sessions/{id}/messages       (ASST) — queue message
GET  /race/sessions/{id}/messages/next  (APP)  — pop pending messages
POST /race/sessions/{id}/messages/{mid}/ack (APP) — acknowledge
```

**Queue a message:**

```bash
curl -X POST http://127.0.0.1:8091/race/sessions/$SID/messages \
  -H "Authorization: Bearer $ASSISTANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Box this lap, fuel is critical",
    "priority": "urgent",
    "expires_in_s": 90,
    "speak_when": "now"
  }'
```

**Pop and ack:**

```bash
# Get pending messages (sorted by priority)
curl http://127.0.0.1:8091/race/sessions/$SID/messages/next \
  -H "Authorization: Bearer $APP_TOKEN"

# Acknowledge delivery
curl -X POST http://127.0.0.1:8091/race/sessions/$SID/messages/$MID/ack \
  -H "Authorization: Bearer $APP_TOKEN"
```

### Export

```bash
curl http://127.0.0.1:8091/race/sessions/$SID/export \
  -H "Authorization: Bearer $ASSISTANT_TOKEN"
```

Returns full JSON with all laps, events, and messages for post-race analysis.

## Project Structure

```
race-worker/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app + lifespan
│   ├── models.py         # Pydantic request/response models
│   ├── database.py       # SQLite WAL setup + connection manager
│   ├── auth.py           # Bearer token auth (two roles)
│   ├── rate_limit.py     # In-memory rate limiter
│   └── routes.py         # All /race/* endpoints
├── tests/
│   ├── conftest.py       # Fixtures (tmp DB, test client, tokens)
│   └── test_api.py       # 20 tests covering all endpoints
├── scripts/
│   └── mock_app.py       # Simulates a full race session
├── deploy/
│   ├── install.sh        # Idempotent server setup
│   ├── race-worker.service
│   ├── env.example
│   └── Caddyfile.snippet
├── requirements.txt
└── README.md
```

---

## Краткое описание (RU)

**race-worker** — легковесный Python-сервис (FastAPI + SQLite), выступающий ретранслятором между приложением голосового инженера iRacing на ПК гонщика и удалённым AI-ассистентом (RaceBot).

**Как это работает:**
- Приложение на ПК отправляет данные о кругах и событиях через POST-запросы
- RaceBot периодически опрашивает GET `/race/sessions/{id}/state` для получения обновлений
- RaceBot ставит в очередь короткие фразы для TTS через POST `/race/sessions/{id}/messages`
- Приложение забирает сообщения через GET `.../messages/next` и подтверждает доставку

**Развёртывание:** Ubuntu 24.04, systemd-юнит, Caddy с TLS, SQLite в WAL-режиме. Два bearer-токена: один для приложения, другой для ассистента. Потребление ~30-50 МБ RAM.

**Запуск тестов:** `python3 -m pytest tests/ -v`

**Эмуляция гонки:** `python3 scripts/mock_app.py --base-url http://127.0.0.1:8091 --app-token TOKEN --assistant-token TOKEN`
