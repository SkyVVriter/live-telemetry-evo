"""FastAPI application entry point for race-worker."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import DB_PATH, init_db
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="race-worker",
    version="0.1.0",
    description="Relay between iRacing voice-engineer app and RaceBot assistant",
    lifespan=lifespan,
)

app.include_router(router)
