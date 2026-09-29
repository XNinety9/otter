"""Endpoints used by the web UI."""

import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .api_device import publish_device
from .db import get_session
from .events import broadcaster, wakeups
from .models import ACTIVE_STATES, Deployment, Device, Firmware
from .schemas import DeploymentOut, DeployIn, DeviceOut, DevicePatch, FirmwareOut
from .storage import delete_firmware_file, store_firmware

router = APIRouter(prefix="/api", tags=["ui"])


@router.get("/config")
def get_config():
    return {"checkin_interval_s": config.CHECKIN_INTERVAL_S}


# --- Devices ---------------------------------------------------------------


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(session: Session = Depends(get_session)):
    return session.scalars(select(Device).order_by(Device.app, Device.name, Device.mac)).all()


@router.get("/devices/{device_id}/deployments", response_model=list[DeploymentOut])
def device_deployments(device_id: int, limit: int = 100, session: Session = Depends(get_session)):
    session.get(Device, device_id) or _404("device")
    return session.scalars(
        select(Deployment).where(Deployment.device_id == device_id).order_by(Deployment.id.desc()).limit(limit)
    ).all()


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def patch_device(device_id: int, body: DevicePatch, session: Session = Depends(get_session)):
    device = session.get(Device, device_id) or _404("device")
    device.name = (body.name or "").strip() or None
    session.commit()
    publish_device(device)
    return device


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int, session: Session = Depends(get_session)):
    device = session.get(Device, device_id) or _404("device")
    session.delete(device)
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
    session: Session = Depends(get_session),
):
    try:
        sha, size = store_firmware(file.file)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    fw = Firmware(app=app.strip(), hw=hw.strip(), version=version.strip(), size=size, sha256=sha, notes=notes)
    session.add(fw)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        _cleanup_orphan(session, sha)
        raise HTTPException(409, f"{app} {version} for {hw} already exists") from None
    broadcaster.publish("firmwares")
    return fw


@router.delete("/firmwares/{firmware_id}", status_code=204)
def delete_firmware(firmware_id: int, session: Session = Depends(get_session)):
    fw = session.get(Firmware, firmware_id) or _404("firmware")
    in_use = session.scalar(
        select(Deployment.id).where(Deployment.firmware_id == fw.id, Deployment.status.in_(ACTIVE_STATES))
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
    devices = session.scalars(select(Device).where(Device.id.in_(body.device_ids))).all()
    if len(devices) != len(set(body.device_ids)):
        raise HTTPException(404, "unknown device")
    if wrong := [d.mac for d in devices if d.hw != fw.hw]:
        raise HTTPException(422, f"firmware is for {fw.hw}, not compatible with {', '.join(wrong)}")

    for device in devices:
        if previous := device.active_deployment:
            previous.status, previous.error = "cancelled", "superseded"
        device.deployments.append(Deployment(firmware=fw))
    session.commit()
    for device in devices:
        publish_device(device)
        wakeups.notify(device.mac)
    return devices


@router.post("/deployments/{deployment_id}/cancel", response_model=DeviceOut)
def cancel_deployment(deployment_id: int, session: Session = Depends(get_session)):
    deployment = session.get(Deployment, deployment_id) or _404("deployment")
    if deployment.status not in ACTIVE_STATES:
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
