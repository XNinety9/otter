from datetime import datetime

from sqlalchemy import Column, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# queued: waiting for a later stage of a rollout, invisible to the device.
ACTIVE_STATES = ("pending", "downloading", "rebooting")
OPEN_STATES = ("queued", *ACTIVE_STATES)
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
    app: Mapped[str]
    fw_version: Mapped[str]
    ip: Mapped[str | None]
    rssi: Mapped[int | None]
    uptime_s: Mapped[int | None]
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)

    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="device", cascade="all, delete-orphan", order_by="Deployment.id"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=device_tags, back_populates="devices", order_by=Tag.name)

    @property
    def active_deployment(self) -> "Deployment | None":
        return next((d for d in reversed(self.deployments) if d.status in ACTIVE_STATES), None)

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
