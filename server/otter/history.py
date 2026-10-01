"""Signal and free memory history (#98): a sample per device every SAMPLE_EVERY, kept KEEP.

A restart forces a sample at once, carrying its reset reason, so the charts can mark it.
"""

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import Device, DeviceSample

SAMPLE_EVERY = timedelta(minutes=5)
KEEP = timedelta(days=7)
PRUNE_EVERY = timedelta(hours=1)

_last_sample: dict[int, object] = {}  # device id -> time of its last sample, this process
_pruned_at = None


def record(session: Session, device: Device, restart: str | None = None) -> None:
    """Called at each check-in; writes a sample when one is due."""
    global _pruned_at
    now = utcnow()
    last = _last_sample.get(device.id)
    if restart is None and (last is not None and now - last < SAMPLE_EVERY):
        return
    if restart is None and device.rssi is None and device.free_heap is None:
        return
    session.add(DeviceSample(device_id=device.id, at=now, rssi=device.rssi, free_heap=device.free_heap, restart=restart))
    _last_sample[device.id] = now
    if _pruned_at is None or now - _pruned_at > PRUNE_EVERY:
        session.execute(delete(DeviceSample).where(DeviceSample.at < now - KEEP))
        _pruned_at = now


def samples(session: Session, device_id: int, hours: int) -> list[DeviceSample]:
    since = utcnow() - timedelta(hours=hours)
    return list(
        session.scalars(
            select(DeviceSample)
            .where(DeviceSample.device_id == device_id, DeviceSample.at >= since)
            .order_by(DeviceSample.at)
        )
    )


def forget(device_id: int) -> None:
    _last_sample.pop(device_id, None)
