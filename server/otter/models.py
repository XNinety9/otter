import json
from datetime import datetime

from sqlalchemy import Column, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# queued: waiting for a later stage of a rollout, invisible to the device.
ACTIVE_STATES = ("pending", "downloading", "rebooting")
OPEN_STATES = ("queued", *ACTIVE_STATES)
def silence_allowed(base_s: float, next_checkin_s: int | None) -> float:
    """How long a device may stay silent before counting as offline: the usual timeout, or
    longer for a device that said when it would check in next (deep sleep, #20)."""
    return max(base_s, next_checkin_s * 1.25 + 60) if next_checkin_s else base_s


# Reset reasons that mean the firmware crashed or the hardware is struggling.
CRASH_RESETS = {"panic", "int_watchdog", "task_watchdog", "watchdog", "brownout", "power_glitch", "cpu_lockup"}
FINAL_STATES = ("success", "failed", "cancelled")


device_tags = Table(
    "device_tags",
    Base.metadata,
    Column("device_id", ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)

    devices: Mapped[list["Device"]] = relationship(secondary=device_tags, back_populates="tags")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    mac: Mapped[str] = mapped_column(String(17), unique=True)
    name: Mapped[str | None]
    hw: Mapped[str]
    chip: Mapped[str | None]  # exact model, e.g. "ESP32-C6FH4 (QFN32)" (#74)
    chip_rev: Mapped[str | None]  # its revision, e.g. "0.1"
    flash_size: Mapped[int | None]  # bytes (#80)
    psram_size: Mapped[int | None]  # bytes, None without PSRAM
    radio: Mapped[str | None]  # e.g. "Wi-Fi 4, Bluetooth 5 (LE)" (#88)
    app: Mapped[str]
    fw_version: Mapped[str]
    ip: Mapped[str | None]
    rssi: Mapped[int | None]
    uptime_s: Mapped[int | None]
    ota_slot_size: Mapped[int | None]  # bytes available for an update image, as the device reports it
    free_heap: Mapped[int | None]
    min_free_heap: Mapped[int | None]  # lowest since boot
    reset_reason: Mapped[str | None]  # why the device last restarted: power_on, panic, brownout…
    boot_count: Mapped[int | None]
    # Authentication (#15, see device_auth.py).
    # A unique index, not a constraint: adding a constraint makes SQLite rebuild the table,
    # and the ON DELETE CASCADE foreign keys would take the deployments with it.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    token_used_at: Mapped[datetime | None]  # once set, the fleet key no longer works for this device
    revoked_at: Mapped[datetime | None]
    approved: Mapped[bool] = mapped_column(default=True, server_default="1")  # False: awaiting approval
    config_version: Mapped[str | None]  # version of the configuration the device reports having (#23)
    next_checkin_s: Mapped[int | None]  # when a sleeping device said it would be back (#20)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    channel: Mapped[str | None]  # follows this release channel automatically; None = manual updates

    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", order_by="Deployment.id"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=device_tags, back_populates="devices", order_by=Tag.name)
    commands: Mapped[list["Command"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", order_by="Command.id"
    )

    @property
    def active_deployment(self) -> "Deployment | None":
        return next((d for d in reversed(self.deployments) if d.status in ACTIVE_STATES), None)

    @property
    def auth(self) -> str:
        """revoked, awaiting_approval, token (uses its own token) or fleet_key."""
        if self.revoked_at is not None:
            return "revoked"
        if not self.approved:
            return "awaiting_approval"
        return "token" if self.token_used_at is not None else "fleet_key"

    def fits(self, firmware: "Firmware") -> bool:
        """Whether the image fits the device's OTA slot (unknown slot size: assume it does)."""
        return self.ota_slot_size is None or firmware.size <= self.ota_slot_size

    @property
    def last_deployment(self) -> "Deployment | None":
        return self.deployments[-1] if self.deployments else None


class Firmware(Base):
    __tablename__ = "firmwares"
    __table_args__ = (UniqueConstraint("app", "hw", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str]
    hw: Mapped[str]
    version: Mapped[str]
    size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str | None]
    uploaded_at: Mapped[datetime] = mapped_column(default=utcnow)
    channel: Mapped[str | None]  # release channel it is published on; None = unpublished
    signature: Mapped[str | None]  # base64, made with the builder's private key (see signing.py)
    # SHA-256 of the ELF file the image was built from (read from ESP32 images), and whether
    # that ELF was uploaded: crash reports are then decoded (#24).
    elf_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    has_elf: Mapped[bool] = mapped_column(default=False, server_default="0")
    compressed_size: Mapped[int | None]  # size of the zlib copy devices download instead (#25)
    # The whole flash (bootloader, partitions, app) to install it on a new board from the
    # browser (#92, see storage.store_factory).
    factory_sha256: Mapped[str | None] = mapped_column(String(64))

    @property
    def has_factory(self) -> bool:
        return self.factory_sha256 is not None

    @property
    def signed(self) -> bool:
        return self.signature is not None


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    firmware_id: Mapped[int] = mapped_column(ForeignKey("firmwares.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(default="pending")
    progress: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    attempts: Mapped[int] = mapped_column(default=1, server_default="1")  # 1 + retries so far
    retry_at: Mapped[datetime | None]  # a retried deployment isn't handed out before this
    rollout_id: Mapped[int | None] = mapped_column(ForeignKey("rollouts.id", ondelete="SET NULL"))
    stage: Mapped[int | None]  # index of the rollout stage this deployment belongs to

    device: Mapped[Device] = relationship(back_populates="deployments")
    firmware: Mapped[Firmware] = relationship(lazy="joined")
    rollout: Mapped["Rollout | None"] = relationship(back_populates="deployments")


class Rollout(Base):
    """A firmware released in stages: each stage starts once the previous one succeeded."""

    __tablename__ = "rollouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    firmware_id: Mapped[int] = mapped_column(ForeignKey("firmwares.id", ondelete="CASCADE"))
    tags: Mapped[str]  # comma-separated, empty = every device of the app and hardware
    channel: Mapped[str | None]  # set when the rollout publishes the firmware on a channel
    stages: Mapped[str]  # JSON list of cumulative percentages, e.g. [10, 50, 100]
    soak_s: Mapped[int]
    max_failure_rate: Mapped[float]
    status: Mapped[str] = mapped_column(default="running")  # running, paused, halted, completed, aborted
    current_stage: Mapped[int] = mapped_column(default=0)
    stage_done_at: Mapped[datetime | None]  # when the current stage finished (soak start)
    message: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    firmware: Mapped[Firmware] = relationship(lazy="joined")
    deployments: Mapped[list[Deployment]] = relationship(back_populates="rollout", order_by=Deployment.id)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str]  # argon2
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuthSession(Base):
    """A browser login. Only a hash of the cookie's token is stored."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]

    user: Mapped[User] = relationship(lazy="joined")


class ApiToken(Base):
    """For scripts, CI and Prometheus: sent as `Authorization: Bearer otk_…`."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(lazy="joined")


class Command(Base):
    """A remote command for a device: queued, delivered with a check-in, then acknowledged."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    name: Mapped[str]
    args: Mapped[str | None]  # JSON object
    status: Mapped[str] = mapped_column(default="queued")  # queued, sent, done, failed, expired
    result: Mapped[str | None]  # the device's message, or why it expired
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]

    device: Mapped[Device] = relationship(back_populates="commands")


class ConfigValue(Base):
    """One remote configuration value (#23), for a device or for a tag (by name: the setting
    outlives the tag being unused for a while). See devconfig.py."""

    __tablename__ = "config_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    tag: Mapped[str | None] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(32))
    value: Mapped[str]  # JSON: a string, number or boolean


class Crash(Base):
    """A crash report (#24): the core dump summary a device sent after restarting from a crash."""

    __tablename__ = "crashes"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    firmware_id: Mapped[int | None] = mapped_column(ForeignKey("firmwares.id", ondelete="SET NULL"), index=True)
    elf_sha256: Mapped[str]  # as reported: often a prefix
    fw_version: Mapped[str | None]  # of the crashed firmware when known, else what the device runs
    task: Mapped[str | None]
    reason: Mapped[str | None]
    report: Mapped[str]  # JSON, as sent (registers, backtrace, stack words)
    frames: Mapped[str]  # JSON: decoded call stack, see symbols.Symbolizer.frame
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    device: Mapped[Device] = relationship()
    firmware: Mapped[Firmware | None] = relationship()

    @property
    def decoded(self) -> bool:
        return any(frame.get("function") for frame in json.loads(self.frames))
