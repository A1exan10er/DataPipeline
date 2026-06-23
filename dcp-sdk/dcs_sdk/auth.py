"""JWT decode helpers shared by lightweight clients and services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from jose import JWTError, jwt

from dcs_sdk.config import get_section


@dataclass(frozen=True)
class TokenUser:
    id: UUID
    role: str
    username: str
    display_name: str = ""


def _jwt_config() -> tuple[str, str]:
    cfg = get_section("auth")
    return cfg.get("secret_key", ""), cfg.get("algorithm", "HS256")


def decode_access_token(token: str) -> Optional[dict]:
    secret_key, algorithm = _jwt_config()
    try:
        return jwt.decode(token, secret_key, algorithms=[algorithm])
    except JWTError:
        return None
