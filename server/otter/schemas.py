from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

UtcDatetime = Annotated[
    datetime, PlainSerializer(lambda dt: dt.replace(tzinfo=UTC).isoformat(), return_type=str)
]


# --- Device protocol -------------------------------------------------------


class CheckinIn(BaseModel):
    mac: str
    hw: str = Field(min_length=1, max_length=32)
    app: str = Field(min_length=1, max_length=64)
    fw_version: str = Field(min_length=1, max_length=32)
    ip: str | None = None
    rssi: int | None = None
    uptime_s: int | None = None
    # Long polling: if no update is ready, the server may hold the request up to this long.
    wait_s: int = Field(default=0, ge=0)

    @field_validator("mac")
    @classmethod
    def normalize_mac(cls, v: str) -> str:
        hexdigits = "".join(c for c in v.lower() if c in "0123456789abcdef")
        if len(hexdigits) != 12:
            raise ValueError("invalid MAC address")
        return ":".join(hexdigits[i : i + 2] for i in range(0, 12, 2))


class UpdateOrder(BaseModel):
    deployment_id: int
    version: str
    url: str
    size: int
    sha256: str


class CheckinOut(BaseModel):
    checkin_interval_s: int
    update: UpdateOrder | None = None


class ProgressIn(BaseModel):
    state: Literal["downloading", "rebooting", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    error: str | None = Field(default=None, max_length=200)


# --- UI API ----------------------------------------------------------------


class FirmwareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    app: str
    hw: str
    version: str
    size: int
    sha256: str
    notes: str | None
    uploaded_at: UtcDatetime


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    status: str
    progress: int
    error: str | None
    firmware: FirmwareOut
    created_at: UtcDatetime
    updated_at: UtcDatetime


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mac: str
    name: str | None
    hw: str
    app: str
    fw_version: str
    ip: str | None
    rssi: int | None
    uptime_s: int | None
    first_seen: UtcDatetime
    last_seen: UtcDatetime
    last_deployment: DeploymentOut | None


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)


class DeployIn(BaseModel):
    firmware_id: int
    device_ids: list[int] = Field(min_length=1)
