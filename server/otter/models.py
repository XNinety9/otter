from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

ACTIVE_STATES = ("pending", "downloading", "rebooting")
FINAL_STATES = ("success", "failed", "cancelled")


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

    device: Mapped[Device] = relationship(back_populates="deployments")
    firmware: Mapped[Firmware] = relationship(lazy="joined")
