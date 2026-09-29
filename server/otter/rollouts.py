"""Staged rollouts.

A rollout freezes its targets when created and splits them into stages (cumulative
percentages, e.g. 10 %, 50 %, 100 %). Deployments of later stages are created as
`queued`, which devices never see. `evaluate()` runs periodically: once every
deployment of the current stage is final and the soak time has elapsed, the next
stage is released. If failures in a stage exceed `max_failure_rate`, the rollout halts
before touching the rest of the fleet.
"""

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import utcnow
from .models import ACTIVE_STATES, FINAL_STATES, OPEN_STATES, Deployment, Device, Firmware, Rollout, Tag, silence_allowed
from .versions import is_newer

RUNNING, PAUSED, HALTED, COMPLETED, ABORTED = "running", "paused", "halted", "completed", "aborted"
OPEN_ROLLOUT_STATES = (RUNNING, PAUSED, HALTED)


class RolloutError(ValueError):
    pass


@dataclass
class Changes:
    """What an operation touched, for the caller to publish after committing."""

    devices: set[int] = field(default_factory=set)  # device ids to republish
    wake: set[str] = field(default_factory=set)  # MACs with a newly pending deployment
    rollouts: bool = False
    halted: list[Rollout] = field(default_factory=list)


def stage_sizes(total: int, percentages: list[int]) -> list[int]:
    """Number of devices in each stage: at least one each, empty stages (rounding) dropped."""
    sizes, done = [], 0
    for pct in percentages:
        upto = total if pct >= 100 else min(total, max(done + 1, math.floor(total * pct / 100)))
        if upto > done:
            sizes.append(upto - done)
            done = upto
    return sizes


def create(
    session: Session,
    fw: Firmware,
    tags: list[str],
    stages: list[int],
    soak_s: int,
    max_failure_rate: float,
    channel: str | None = None,
) -> tuple[Rollout, Changes]:
    """Targets devices of the tags, or following the channel, or all of the app and hardware.

    With a channel, the firmware is also published on it, and only devices on an older
    version are targeted (channels never downgrade). Otherwise any other version is a
    target, so a rollout can also roll a fleet back.
    """
    from .channels import followers  # channels imports this module

    query = select(Device).where(Device.app == fw.app, Device.hw == fw.hw, Device.fw_version != fw.version)
    if tags:
        query = query.where(Device.tags.any(Tag.name.in_(tags)))
    if channel:
        query = query.where(followers(channel))
    targets = list(session.scalars(query).all())
    if channel:
        targets = [d for d in targets if is_newer(fw.version, d.fw_version)]
    scope = f" tagged {', '.join(tags)}" if tags else f" following {channel}" if channel else ""
    if not targets:
        raise RolloutError(f"no {fw.app} / {fw.hw} device{scope} needs {fw.version}")
    targets = [d for d in targets if d.fits(fw)]
    if not targets:
        raise RolloutError(f"{fw.app} {fw.version} is too big for the OTA slot of every device{scope}")
    if channel:
        fw.channel = channel

    # Canaries should answer quickly: online devices first, in random order.
    random.shuffle(targets)
    now = utcnow()
    targets.sort(key=lambda d: (now - d.last_seen).total_seconds() >= silence_allowed(config.ONLINE_TIMEOUT_S, d.next_checkin_s))

    sizes = stage_sizes(len(targets), stages)
    rollout = Rollout(
        firmware=fw,
        tags=",".join(tags),
        channel=channel,
        stages=json.dumps(stages),
        soak_s=soak_s,
        max_failure_rate=max_failure_rate,
    )
    session.add(rollout)
    changes = Changes(rollouts=True)
    start = 0
    for stage, size in enumerate(sizes):
        for device in targets[start : start + size]:
            cancel_open_deployments(device, "superseded")
            device.deployments.append(
                Deployment(firmware=fw, rollout=rollout, stage=stage, status="pending" if stage == 0 else "queued")
            )
            changes.devices.add(device.id)
            if stage == 0:
                changes.wake.add(device.mac)
        start += size
    session.flush()
    return rollout, changes


def cancel_open_deployments(device: Device, reason: str) -> None:
    for deployment in device.deployments:
        if deployment.status in OPEN_STATES:
            deployment.status, deployment.error = "cancelled", reason


def stage_deployments(rollout: Rollout, stage: int) -> list[Deployment]:
    return [d for d in rollout.deployments if d.stage == stage]


def stage_count(rollout: Rollout) -> int:
    return max((d.stage for d in rollout.deployments if d.stage is not None), default=-1) + 1


def start_stage(rollout: Rollout, stage: int, changes: Changes) -> None:
    rollout.current_stage = stage
    rollout.stage_done_at = None
    rollout.status = RUNNING
    rollout.message = None
    for deployment in stage_deployments(rollout, stage):
        if deployment.status == "queued":
            deployment.status = "pending"
            changes.devices.add(deployment.device_id)
            changes.wake.add(deployment.device.mac)
    changes.rollouts = True


def evaluate(session: Session, now: datetime | None = None) -> Changes:
    """Advances or halts every running rollout. Call periodically."""
    now = now or utcnow()
    changes = Changes()
    for rollout in session.scalars(select(Rollout).where(Rollout.status == RUNNING)).all():
        deployments = stage_deployments(rollout, rollout.current_stage)
        failed = sum(d.status == "failed" for d in deployments)
        allowed = math.floor(len(deployments) * rollout.max_failure_rate)
        if failed > allowed:
            rollout.status = HALTED
            rollout.message = f"stage {rollout.current_stage + 1}: {failed}/{len(deployments)} failed"
            changes.rollouts = True
            changes.halted.append(rollout)
            continue
        if any(d.status not in FINAL_STATES for d in deployments):
            continue
        if rollout.stage_done_at is None:
            rollout.stage_done_at = now
            changes.rollouts = True
        if rollout.current_stage + 1 >= stage_count(rollout):
            rollout.status = COMPLETED
            changes.rollouts = True
        elif now >= rollout.stage_done_at + timedelta(seconds=rollout.soak_s):
            start_stage(rollout, rollout.current_stage + 1, changes)
    return changes


def pause(rollout: Rollout) -> Changes:
    if rollout.status != RUNNING:
        raise RolloutError(f"rollout is {rollout.status}")
    rollout.status = PAUSED
    return Changes(rollouts=True)


def resume(rollout: Rollout) -> Changes:
    if rollout.status != PAUSED:
        raise RolloutError(f"rollout is {rollout.status}")
    rollout.status = RUNNING
    return Changes(rollouts=True)


def advance(rollout: Rollout) -> Changes:
    """Starts the next stage now, whatever the state of the current one."""
    if rollout.status not in OPEN_ROLLOUT_STATES:
        raise RolloutError(f"rollout is {rollout.status}")
    if rollout.current_stage + 1 >= stage_count(rollout):
        raise RolloutError("already at the last stage")
    changes = Changes()
    start_stage(rollout, rollout.current_stage + 1, changes)
    return changes


def abort(rollout: Rollout) -> Changes:
    """Stops the rollout and cancels its deployments that haven't started flashing."""
    if rollout.status not in OPEN_ROLLOUT_STATES:
        raise RolloutError(f"rollout is {rollout.status}")
    rollout.status = ABORTED
    changes = Changes(rollouts=True)
    for deployment in rollout.deployments:
        if deployment.status in ("queued", "pending", "downloading"):
            deployment.status, deployment.error = "cancelled", "rollout aborted"
            changes.devices.add(deployment.device_id)
    return changes


def stats(rollout: Rollout) -> list[dict]:
    """Per-stage counts for the UI."""
    result = []
    for stage in range(stage_count(rollout)):
        deployments = stage_deployments(rollout, stage)
        result.append(
            {
                "size": len(deployments),
                "success": sum(d.status == "success" for d in deployments),
                "failed": sum(d.status == "failed" for d in deployments),
                "cancelled": sum(d.status == "cancelled" for d in deployments),
                "active": sum(d.status in ACTIVE_STATES for d in deployments),
                "queued": sum(d.status == "queued" for d in deployments),
            }
        )
    return result
