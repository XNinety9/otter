"""Device authentication (#15): the fleet key enrolls, then each device uses its own token.

A device that checks in with the fleet key (X-Otter-Key) gets a token in the response, and
sends it afterwards as `Authorization: Bearer otd_…`. Once a device has used its token, the
fleet key no longer works for it: an extracted fleet key can't impersonate it, and revoking
it blocks it alone. Agents that ignore tokens keep working with the fleet key (a new token
is issued at each of their check-ins, unused). Only SHA-256 hashes of tokens are stored.
"""

import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select

from . import config
from .auth import token_hash
from .db import SessionLocal, utcnow
from .models import Device

TOKEN_PREFIX = "otd_"


@dataclass(frozen=True)
class DeviceAuth:
    device_id: int | None  # set when authenticated by a device token
    fleet: bool  # authenticated by the fleet key (or none is configured)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def authenticate(request: Request) -> DeviceAuth:
    """FastAPI dependency of the device API."""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        with SessionLocal() as session:
            device = session.scalar(select(Device).where(Device.token_hash == token_hash(authorization[7:].strip())))
            if device is None:
                raise HTTPException(401, "invalid device token")
            if device.revoked_at is not None:
                raise HTTPException(403, "device revoked in Otter")
            if device.token_used_at is None:
                device.token_used_at = utcnow()  # from now on the fleet key isn't enough for it
                session.commit()
            return DeviceAuth(device_id=device.id, fleet=False)

    if config.FLEET_KEY:
        # Header for normal calls; query param as a fallback for OTA clients that can't set headers.
        given = request.headers.get("x-otter-key") or request.query_params.get("key") or ""
        if not secrets.compare_digest(given, config.FLEET_KEY):
            raise HTTPException(401, "invalid fleet key")
    return DeviceAuth(device_id=None, fleet=True)


def check_access(auth: DeviceAuth, device: Device) -> None:
    """Whether these credentials may act as (or for) this device."""
    if device.revoked_at is not None:
        raise HTTPException(403, "device revoked in Otter")
    if auth.device_id is not None:
        if auth.device_id != device.id:
            raise HTTPException(403, "this token belongs to another device")
    elif device.token_used_at is not None:
        raise HTTPException(401, "this device authenticates with its own token")
