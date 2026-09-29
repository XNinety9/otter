"""Remote configuration (#23): key/value settings per tag and per device.

A device's effective configuration merges the values of its tags (in alphabetical order) and
then its own, which win. It has a version (a hash); the device sends the version it has in its
check-in and gets the whole configuration back whenever they differ. See "Configuration" in
docs/protocol.md.
"""

import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import ConfigValue, Device

Value = str | int | float | bool


def version(values: dict[str, Value]) -> str:
    """"" for an empty configuration, so devices that never had one don't need an answer."""
    if not values:
        return ""
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def scope_values(session: Session, *, device: Device | None = None, tag: str | None = None) -> dict[str, Value]:
    query = select(ConfigValue).order_by(ConfigValue.key)
    query = query.where(ConfigValue.device_id == device.id) if device else query.where(ConfigValue.tag == tag)
    return {row.key: json.loads(row.value) for row in session.scalars(query)}


def effective(session: Session, device: Device) -> tuple[dict[str, Value], dict[str, str]]:
    """(values, where each value comes from: "#tag" or "device")."""
    values: dict[str, Value] = {}
    sources: dict[str, str] = {}
    for tag in sorted(t.name for t in device.tags):
        for key, value in scope_values(session, tag=tag).items():
            values[key], sources[key] = value, f"#{tag}"
    for key, value in scope_values(session, device=device).items():
        values[key], sources[key] = value, "device"
    return dict(sorted(values.items())), sources


def replace(session: Session, values: dict[str, Value], *, device: Device | None = None, tag: str | None = None) -> None:
    """Sets the values of a scope (a device or a tag), removing the keys not given."""
    owner = ConfigValue.device_id == device.id if device else ConfigValue.tag == tag
    session.execute(delete(ConfigValue).where(owner))
    for key, value in values.items():
        session.add(ConfigValue(device_id=device.id if device else None, tag=tag, key=key, value=json.dumps(value)))


def devices_of_tag(session: Session, tag: str) -> list[Device]:
    from .models import Tag

    return list(session.scalars(select(Device).where(Device.tags.any(Tag.name == tag))))
