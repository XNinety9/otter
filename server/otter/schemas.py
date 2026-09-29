from datetime import UTC, datetime
from typing import Annotated, Literal

import json
import re

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def normalize_tag(value: str) -> str:
    tag = value.strip().lower().replace(" ", "-")
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"invalid tag {value!r}: 1-32 chars, a-z, 0-9, '-' or '_'")
    return tag


TagName = Annotated[str, AfterValidator(normalize_tag)]
ChannelName = TagName  # same rules: lowercase, a-z 0-9 - _

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
    ota_slot_size: int | None = Field(default=None, ge=0)
    free_heap: int | None = Field(default=None, ge=0)
    min_free_heap: int | None = Field(default=None, ge=0)
    reset_reason: str | None = Field(default=None, max_length=32, pattern=r"^[a-z0-9_]+$")
    boot_count: int | None = Field(default=None, ge=0)
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


class CommandOrder(BaseModel):
    id: int
    name: str
    args: dict | None = None


class CheckinOut(BaseModel):
    checkin_interval_s: int
    update: UpdateOrder | None = None
    commands: list[CommandOrder] = []


CommandName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class CommandIn(BaseModel):
    device_ids: list[int] = Field(min_length=1, max_length=500)
    name: CommandName
    args: dict | None = None

    @field_validator("args")
    @classmethod
    def small_args(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v)) > 512:
            raise ValueError("args must be at most 512 bytes of JSON")
        return v


class CommandResultIn(BaseModel):
    ok: bool
    message: str | None = Field(default=None, max_length=200)


class CommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    name: str
    args: Annotated[dict | None, BeforeValidator(lambda v: json.loads(v) if isinstance(v, str) else v)]
    status: str
    result: str | None
    created_at: UtcDatetime
    sent_at: UtcDatetime | None
    done_at: UtcDatetime | None


class ProgressIn(BaseModel):
    state: Literal["downloading", "rebooting", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    error: str | None = Field(default=None, max_length=200)
    # For failures: whether trying again may succeed. Omitted = guessed from `error`.
    retryable: bool | None = None


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
    channel: str | None = None


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
    rollout_id: int | None = None
    stage: int | None = None
    attempts: int = 1


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
    ota_slot_size: int | None = None
    free_heap: int | None = None
    min_free_heap: int | None = None
    reset_reason: str | None = None
    boot_count: int | None = None
    first_seen: UtcDatetime
    last_seen: UtcDatetime
    last_deployment: DeploymentOut | None
    tags: Annotated[list[str], BeforeValidator(lambda tags: [getattr(t, "name", t) for t in tags])]
    channel: str | None = None


class DevicePatch(BaseModel):
    """Only the fields sent are changed."""

    name: str | None = Field(default=None, max_length=64)
    tags: list[TagName] | None = Field(default=None, max_length=20)
    channel: ChannelName | None = None


class FirmwarePatch(BaseModel):
    """Publishes the firmware on a channel (None = unpublish)."""

    channel: ChannelName | None = None


class TagOut(BaseModel):
    name: str
    devices: int


class DeployIn(BaseModel):
    """Targets the given devices, plus every device of the given tags running the firmware's app."""

    firmware_id: int
    device_ids: list[int] = []
    tags: list[TagName] = []

    @model_validator(mode="after")
    def has_targets(self) -> "DeployIn":
        if not self.device_ids and not self.tags:
            raise ValueError("give device_ids and/or tags")
        return self


class RolloutIn(BaseModel):
    firmware_id: int
    tags: list[TagName] = []
    channel: ChannelName | None = None  # target its followers, and publish the firmware on it
    stages: list[int] = Field(default=[10, 50, 100], min_length=1, max_length=10)
    soak_s: int = Field(default=300, ge=0, le=7 * 86400)
    max_failure_rate: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def one_target(self) -> "RolloutIn":
        if self.tags and self.channel:
            raise ValueError("target tags or a channel, not both")
        return self

    @field_validator("stages")
    @classmethod
    def check_stages(cls, stages: list[int]) -> list[int]:
        if any(not 1 <= s <= 100 for s in stages) or stages != sorted(set(stages)) or stages[-1] != 100:
            raise ValueError("stages are increasing percentages ending with 100, e.g. [10, 50, 100]")
        return stages


class RolloutStageOut(BaseModel):
    size: int
    success: int
    failed: int
    cancelled: int
    active: int
    queued: int


class RolloutOut(BaseModel):
    id: int
    firmware: FirmwareOut
    tags: list[str]
    channel: str | None
    stages: list[RolloutStageOut]
    current_stage: int
    status: str
    message: str | None
    soak_s: int
    max_failure_rate: float
    created_at: UtcDatetime
    updated_at: UtcDatetime
    next_stage_at: UtcDatetime | None
