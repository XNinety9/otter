"""Endpoints used by the web UI."""

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import commands, config, signing
from .api_device import publish_device
from .api_rollouts import reconcile_channels
from .auth import require_user
from .db import get_session, utcnow
from .events import broadcaster, wakeups
from .notify import INFO, Message, notifier
from .models import OPEN_STATES, Command, Deployment, Device, Firmware, Tag, device_tags
from .rollouts import cancel_open_deployments
from .schemas import CommandIn, CommandOut, DeploymentOut, DeployIn, DeviceOut, DevicePatch, FirmwareOut, FirmwarePatch, TagOut, normalize_tag
from .storage import delete_firmware_file, firmware_path, store_firmware

router = APIRouter(prefix="/api", tags=["ui"], dependencies=[Depends(require_user)])


@router.get("/config")
def get_config():
    return {"checkin_interval_s": config.CHECKIN_INTERVAL_S, "deploy_attempts": config.DEPLOY_ATTEMPTS}


@router.get("/notifications")
def notification_settings():
    """Configured targets, described without their URLs (they often embed secrets)."""
    return {
        "targets": [t.describe() for t in notifier.targets()],
        "offline_minutes": config.NOTIFY_OFFLINE_MINUTES,
    }


@router.post("/notifications/test")
def test_notifications():
    """Sends a test message to every target right away and reports how each one went."""
    if not notifier.targets():
        raise HTTPException(409, "no notification target configured (OTTER_NOTIFY_URLS)")
    msg = Message("test", "Otter test notification", "Notifications from Otter reach you. 🦦", INFO)
    return {"results": notifier.send(msg)}


# --- Devices ---------------------------------------------------------------


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(session: Session = Depends(get_session)):
    return session.scalars(
        select(Device)
        .options(selectinload(Device.tags), selectinload(Device.deployments))
        .order_by(Device.app, Device.name, Device.mac)
    ).all()


@router.get("/devices/{device_id}/deployments", response_model=list[DeploymentOut])
def device_deployments(device_id: int, limit: int = 100, session: Session = Depends(get_session)):
    session.get(Device, device_id) or _404("device")
    return session.scalars(
        select(Deployment).where(Deployment.device_id == device_id).order_by(Deployment.id.desc()).limit(limit)
    ).all()


# --- Device credentials (#15, see device_auth.py) ---------------------------


@router.post("/devices/{device_id}/revoke", response_model=DeviceOut)
def revoke_device(device_id: int, session: Session = Depends(get_session)):
    """Blocks the device, with its token or the fleet key, until it is re-enrolled."""
    device = session.get(Device, device_id) or _404("device")
    device.revoked_at = utcnow()
    device.token_hash = None
    session.commit()
    publish_device(device)
    wakeups.notify(device.mac)  # end a check-in it may be holding open
    return device


@router.post("/devices/{device_id}/reenroll", response_model=DeviceOut)
def reenroll_device(device_id: int, session: Session = Depends(get_session)):
    """Forgets the device's token: its next check-in with the fleet key gets a new one."""
    device = session.get(Device, device_id) or _404("device")
    device.revoked_at = device.token_used_at = device.token_hash = None
    device.approved = True
    session.commit()
    publish_device(device)
    return device


@router.post("/devices/{device_id}/approve", response_model=DeviceOut)
def approve_device(device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id) or _404("device")
    device.approved = True
    session.commit()
    publish_device(device)
    return device


@router.get("/devices/{device_id}/commands", response_model=list[CommandOut])
def device_commands(device_id: int, limit: int = 20, session: Session = Depends(get_session)):
    session.get(Device, device_id) or _404("device")
    return session.scalars(
        select(Command).where(Command.device_id == device_id).order_by(Command.id.desc()).limit(limit)
    ).all()


@router.post("/commands", response_model=list[CommandOut], status_code=201)
def send_commands(body: CommandIn, session: Session = Depends(get_session)):
    """Queues a command for each device; long-polling devices get it within a second or two."""
    devices = session.scalars(select(Device).where(Device.id.in_(body.device_ids))).all()
    if len(devices) != len(set(body.device_ids)):
        raise HTTPException(404, "unknown device")
    queued = [commands.queue(device, body.name, body.args) for device in devices]
    session.commit()
    for device, command in zip(devices, queued):
        commands.publish(command)
        wakeups.notify(device.mac)
    return queued


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def patch_device(device_id: int, body: DevicePatch, session: Session = Depends(get_session)):
    device = session.get(Device, device_id) or _404("device")
    if "name" in body.model_fields_set:
        device.name = (body.name or "").strip() or None
    if "tags" in body.model_fields_set:
        device.tags = [get_or_create_tag(session, name) for name in sorted(set(body.tags or []))]
        session.flush()
        delete_unused_tags(session)
    if "channel" in body.model_fields_set:
        device.channel = body.channel
    session.commit()
    publish_device(device)
    if "channel" in body.model_fields_set:
        reconcile_channels()  # a newer firmware may be waiting on that channel
        session.refresh(device)
    return device


def get_or_create_tag(session: Session, name: str) -> Tag:
    tag = session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name)
        session.add(tag)
    return tag


def delete_unused_tags(session: Session) -> None:
    session.execute(delete(Tag).where(~Tag.devices.any()))


@router.get("/tags", response_model=list[TagOut])
def list_tags(session: Session = Depends(get_session)):
    rows = session.execute(
        select(Tag.name, func.count(device_tags.c.device_id))
        .join(device_tags, isouter=True)
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    return [TagOut(name=name, devices=count) for name, count in rows]


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id) or _404("device")
    session.delete(device)
    session.flush()
    delete_unused_tags(session)
    session.commit()
    broadcaster.publish("device_deleted", {"id": device_id})


# --- Firmwares -------------------------------------------------------------


@router.get("/firmwares", response_model=list[FirmwareOut])
def list_firmwares(session: Session = Depends(get_session)):
    return session.scalars(select(Firmware).order_by(Firmware.uploaded_at.desc())).all()


@router.post("/firmwares", response_model=FirmwareOut, status_code=201)
def upload_firmware(
    file: UploadFile,
    app: str = Form(min_length=1, max_length=64),
    hw: str = Form(min_length=1, max_length=32),
    version: str = Form(min_length=1, max_length=32),
    notes: str | None = Form(default=None),
    channel: str | None = Form(default=None),
    signature: str | None = Form(default=None, max_length=2048),
    session: Session = Depends(get_session),
):
    try:
        sha, size = store_firmware(file.file)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    signature = (signature or "").strip() or None
    try:
        if signature:
            signing.decode(signature)
        if key := signing.server_key():
            if not signature:
                raise ValueError("unsigned firmware refused: this server requires signed images")
            signing.verify(key, firmware_path(sha), signature)
    except ValueError as exc:
        _cleanup_orphan(session, sha)
        raise HTTPException(422, str(exc)) from exc

    try:
        channel = normalize_tag(channel) if channel and channel.strip() else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    fw = Firmware(
        app=app.strip(), hw=hw.strip(), version=version.strip(), size=size, sha256=sha, notes=notes, channel=channel,
        signature=signature,
    )
    session.add(fw)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        _cleanup_orphan(session, sha)
        raise HTTPException(409, f"{app} {version} for {hw} already exists") from None
    broadcaster.publish("firmwares")
    if fw.channel:
        reconcile_channels()
    return fw


@router.patch("/firmwares/{firmware_id}", response_model=FirmwareOut)
def patch_firmware(firmware_id: int, body: FirmwarePatch, session: Session = Depends(get_session)):
    """Publishes the firmware on a release channel: its followers are updated right away."""
    fw = session.get(Firmware, firmware_id) or _404("firmware")
    fw.channel = body.channel
    session.commit()
    broadcaster.publish("firmwares")
    reconcile_channels()
    return fw


@router.delete("/firmwares/{firmware_id}", status_code=204)
def delete_firmware(firmware_id: int, session: Session = Depends(get_session)):
    fw = session.get(Firmware, firmware_id) or _404("firmware")
    in_use = session.scalar(
        select(Deployment.id).where(Deployment.firmware_id == fw.id, Deployment.status.in_(OPEN_STATES))
    )
    if in_use:
        raise HTTPException(409, "firmware is being deployed")
    session.delete(fw)
    session.commit()
    _cleanup_orphan(session, fw.sha256)
    broadcaster.publish("firmwares")
    broadcaster.publish("resync")


def _cleanup_orphan(session: Session, sha: str) -> None:
    if not session.scalar(select(Firmware.id).where(Firmware.sha256 == sha)):
        delete_firmware_file(sha)


# --- Deployments -----------------------------------------------------------


@router.post("/deployments", response_model=list[DeviceOut], status_code=201)
def create_deployments(body: DeployIn, session: Session = Depends(get_session)):
    fw = session.get(Firmware, body.firmware_id) or _404("firmware")
    devices = list(session.scalars(select(Device).where(Device.id.in_(body.device_ids))).all())
    if len(devices) != len(set(body.device_ids)):
        raise HTTPException(404, "unknown device")
    if wrong := [d.mac for d in devices if d.hw != fw.hw]:
        raise HTTPException(422, f"firmware is for {fw.hw}, not compatible with {', '.join(wrong)}")
    if too_small := [d for d in devices if not d.fits(fw)]:
        names = ", ".join(f"{d.name or d.mac} ({d.ota_slot_size:,} bytes)" for d in too_small)
        raise HTTPException(422, f"{fw.app} {fw.version} ({fw.size:,} bytes) doesn't fit the OTA slot of {names}")

    if body.tags:
        # A tag can mix hardware and apps: only target the devices this firmware is built for,
        # and skip those already running it.
        tagged = session.scalars(
            select(Device)
            .where(Device.tags.any(Tag.name.in_(body.tags)), Device.app == fw.app, Device.hw == fw.hw)
            .order_by(Device.id)
        ).all()
        tag_list = ", ".join(body.tags)
        if not tagged:
            raise HTTPException(422, f"no {fw.app} / {fw.hw} device tagged {tag_list}")
        outdated = [d for d in tagged if d.fw_version != fw.version]
        if not outdated and not devices:
            raise HTTPException(422, f"every {fw.app} device tagged {tag_list} already runs {fw.version}")
        outdated = [d for d in outdated if d.fits(fw)]  # skipped: the UI shows their slot size
        if not outdated and not devices:
            raise HTTPException(422, f"{fw.app} {fw.version} is too big for every device tagged {tag_list}")
        devices += [d for d in outdated if d not in devices]

    for device in devices:
        # Also drops a queued rollout deployment, which would otherwise override this one later.
        cancel_open_deployments(device, "superseded")
        device.deployments.append(Deployment(firmware=fw))
    session.commit()
    for device in devices:
        publish_device(device)
        wakeups.notify(device.mac)
    return devices


@router.post("/deployments/{deployment_id}/cancel", response_model=DeviceOut)
def cancel_deployment(deployment_id: int, session: Session = Depends(get_session)):
    deployment = session.get(Deployment, deployment_id) or _404("deployment")
    if deployment.status not in OPEN_STATES:
        raise HTTPException(409, f"deployment is already {deployment.status}")
    deployment.status = "cancelled"
    session.commit()
    publish_device(deployment.device)
    return deployment.device


# --- Live updates ----------------------------------------------------------


@router.get("/events")
async def events(request: Request):
    queue = broadcaster.subscribe()

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _404(what: str):
    raise HTTPException(404, f"unknown {what}")
