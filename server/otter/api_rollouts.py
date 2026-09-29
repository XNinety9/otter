"""Endpoints for staged rollouts (see otter/rollouts.py)."""

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import rollouts
from .api_device import publish_device
from .db import SessionLocal, get_session
from .events import broadcaster, wakeups
from .models import Device, Firmware, Rollout
from .notify import notifier
from .schemas import RolloutIn, RolloutOut

router = APIRouter(prefix="/api", tags=["ui"])


def rollout_out(rollout: Rollout) -> RolloutOut:
    stats = rollouts.stats(rollout)
    next_stage_at = None
    if rollout.status == rollouts.RUNNING and rollout.stage_done_at and rollout.current_stage + 1 < len(stats):
        next_stage_at = rollout.stage_done_at + timedelta(seconds=rollout.soak_s)
    return RolloutOut(
        id=rollout.id,
        firmware=rollout.firmware,
        tags=[t for t in rollout.tags.split(",") if t],
        stages=stats,
        current_stage=rollout.current_stage,
        status=rollout.status,
        message=rollout.message,
        soak_s=rollout.soak_s,
        max_failure_rate=rollout.max_failure_rate,
        created_at=rollout.created_at,
        updated_at=rollout.updated_at,
        next_stage_at=next_stage_at,
    )


def publish(session: Session, changes: rollouts.Changes) -> None:
    """Call after committing: pushes the changes to the UI and wakes long-polling devices."""
    for device_id in changes.devices:
        if device := session.get(Device, device_id):
            publish_device(device)
    for mac in changes.wake:
        wakeups.notify(mac)
    if changes.rollouts:
        broadcaster.publish("rollouts")


def evaluate_now() -> rollouts.Changes:
    """One evaluation pass, run periodically by the app."""
    with SessionLocal() as session:
        changes = rollouts.evaluate(session)
        session.commit()
        publish(session, changes)
        for rollout in changes.halted:
            notifier.emit("rollout_halted", rollout_id=rollout.id)
        return changes


@router.get("/rollouts", response_model=list[RolloutOut])
def list_rollouts(limit: int = 20, session: Session = Depends(get_session)):
    query = select(Rollout).options(selectinload(Rollout.deployments)).order_by(Rollout.id.desc()).limit(limit)
    return [rollout_out(r) for r in session.scalars(query).all()]


@router.post("/rollouts", response_model=RolloutOut, status_code=201)
def create_rollout(body: RolloutIn, session: Session = Depends(get_session)):
    fw = session.get(Firmware, body.firmware_id)
    if fw is None:
        raise HTTPException(404, "unknown firmware")
    try:
        rollout, changes = rollouts.create(
            session, fw, sorted(set(body.tags)), body.stages, body.soak_s, body.max_failure_rate
        )
    except rollouts.RolloutError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    publish(session, changes)
    return rollout_out(rollout)


ACTIONS = {
    "pause": rollouts.pause,
    "resume": rollouts.resume,
    "advance": rollouts.advance,
    "abort": rollouts.abort,
}


@router.post("/rollouts/{rollout_id}/{action}", response_model=RolloutOut)
def rollout_action(rollout_id: int, action: str, session: Session = Depends(get_session)):
    if action not in ACTIONS:
        raise HTTPException(404, f"unknown action {action}")
    rollout = session.get(Rollout, rollout_id)
    if rollout is None:
        raise HTTPException(404, "unknown rollout")
    try:
        changes = ACTIONS[action](rollout)
    except rollouts.RolloutError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    publish(session, changes)
    return rollout_out(rollout)
