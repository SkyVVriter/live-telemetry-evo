"""Bearer-token authentication with two roles: app and assistant."""

from __future__ import annotations

import os
from enum import Enum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer()


class Role(str, Enum):
    app = "app"
    assistant = "assistant"


def _tokens() -> dict[str, Role]:
    mapping: dict[str, Role] = {}
    app_tok = os.environ.get("APP_TOKEN", "")
    asst_tok = os.environ.get("ASSISTANT_TOKEN", "")
    if app_tok:
        mapping[app_tok] = Role.app
    if asst_tok:
        mapping[asst_tok] = Role.assistant
    return mapping


def _get_role(creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)]) -> Role:
    tokens = _tokens()
    role = tokens.get(creds.credentials)
    if role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return role


def require_app(role: Annotated[Role, Depends(_get_role)]) -> Role:
    if role != Role.app:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="App token required")
    return role


def require_assistant(role: Annotated[Role, Depends(_get_role)]) -> Role:
    if role != Role.assistant:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Assistant token required")
    return role


def require_any(role: Annotated[Role, Depends(_get_role)]) -> Role:
    return role
