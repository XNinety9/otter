"""Release channels.

A firmware can be published on a channel ("stable", "beta"…) and a device can follow one.
A device follows its channel *and* "stable": a beta tester gets the newest of both, so it
doesn't stay behind when a version is promoted from beta to stable.

`reconcile()` deploys to each following device the newest firmware of its channels for its
app and hardware, when it is newer than what the device runs. It never downgrades, leaves
devices with an open deployment alone (manual, or queued in a staged rollout), and doesn't
retry a firmware that already failed or was cancelled on a device.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import OPEN_STATES, Deployment, Device, Firmware
from .rollouts import Changes
from .versions import is_newer, version_key

STABLE = "stable"


def channels_of(device: Device) -> set[str]:
    return {device.channel, STABLE}


def followers(firmware_channel: str):
    """SQL condition: devices that receive firmware published on this channel."""
    if firmware_channel == STABLE:
        return Device.channel.is_not(None)
    return Device.channel == firmware_channel


def reconcile(session: Session) -> Changes:
    changes = Changes()
    devices = session.scalars(
        select(Device).where(Device.channel.is_not(None)).options(selectinload(Device.deployments))
    ).all()
    if not devices:
        return changes
    published = session.scalars(select(Firmware).where(Firmware.channel.is_not(None))).all()

    for device in devices:
        candidates = [
            f for f in published if f.app == device.app and f.hw == device.hw and f.channel in channels_of(device)
        ]
        if not candidates:
            continue
        newest = max(candidates, key=lambda f: version_key(f.version))
        if not is_newer(newest.version, device.fw_version):
            continue
        if any(d.status in OPEN_STATES for d in device.deployments):
            continue
        if any(d.firmware_id == newest.id and d.status in ("failed", "cancelled") for d in device.deployments):
            continue  # already tried: redeploy it by hand if needed
        device.deployments.append(Deployment(firmware=newest))
        changes.devices.add(device.id)
        changes.wake.add(device.mac)
    return changes
