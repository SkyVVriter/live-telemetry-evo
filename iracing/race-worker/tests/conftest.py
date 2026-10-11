"""Shared test fixtures."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ["APP_TOKEN"] = "test-app-token"
os.environ["ASSISTANT_TOKEN"] = "test-assistant-token"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["RACE_WORKER_DB"] = db_path
    import app.database as dbmod
    dbmod.DB_PATH = db_path
    dbmod.init_db(db_path)
    yield
    if "RACE_WORKER_DB" in os.environ:
        del os.environ["RACE_WORKER_DB"]


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def app_headers():
    return {"Authorization": "Bearer test-app-token"}


@pytest.fixture
def assistant_headers():
    return {"Authorization": "Bearer test-assistant-token"}
