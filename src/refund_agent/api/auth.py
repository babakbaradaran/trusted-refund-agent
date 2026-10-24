"""API-key login with two roles: agent (start and view runs) and supervisor (also approve or reject)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

ROLE_RANK = {"agent": 1, "supervisor": 2}


@dataclass(frozen=True)
class Caller:
    key_hint: str
    role: str


def current_caller(request: Request, x_api_key: str | None = Header(default=None)) -> Caller:
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-API-Key header")
    role = request.app.state.settings.api_keys.get(x_api_key)
    if role is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown API key")
    fingerprint = hashlib.sha256(x_api_key.encode()).hexdigest()[:8]  # identifies the key without revealing it
    return Caller(key_hint=f"{role}:{fingerprint}", role=role)


def require(role: str):
    def check(caller: Caller = Depends(current_caller)) -> Caller:
        if ROLE_RANK.get(caller.role, 0) < ROLE_RANK[role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{role}'")
        return caller
    return check
