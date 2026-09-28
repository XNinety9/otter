"""Endpoints called by devices. See docs/protocol.md."""

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import SessionLocal, get_session, utcnow
from .events import broadcaster, wakeups
from .models import Deployment, Device, Firmware
from .schemas import CheckinIn, CheckinOut, DeviceOut, ProgressIn, UpdateOrder
from .storage import firmware_path


def require_fleet_key(request: Request) -> None:
    if not config.FLEET_KEY:
        return
    # Header for normal calls; query param as a fallback for OTA clients that can't set headers.
    given = request.headers.get("x-otter-key") or request.query_params.get("key") or ""
    if not secrets.compare_digest(given, config.FLEET_KEY):
        raise HTTPException(401, "invalid fleet key")


# Longest a check-in may be held open (long polling).
MAX_WAIT_S = 60

router = APIRouter(prefix="/api/v1", tags=["device"], dependencies=[Depends(require_fleet_key)])


def publish_device(device: Device) -> None:
    broadcaster.publish("device", DeviceOut.model_validate(device).model_dump(mode="json"))


@router.post("/checkin", response_model=CheckinOut)
async def checkin(body: CheckinIn, request: Request):
    with wakeups.watch(body.mac) as scheduled:
        order = await run_in_threadpool(record_checkin, body, request)
        if order is None and body.wait_s:
            try:
                await asyncio.wait_for(scheduled.wait(), min(body.wait_s, MAX_WAIT_S))
                # Something was scheduled while we waited: re-evaluate to build the order.
                order = await run_in_threadpool(record_checkin, body, request)
            except TimeoutError:
                pass
    return CheckinOut(checkin_interval_s=config.CHECKIN_INTERVAL_S, update=order)


def record_checkin(body: CheckinIn, request: Request) -> UpdateOrder | None:
    with SessionLocal() as session:
        device = session.scalar(select(Device).where(Device.mac == body.mac))
        if device is None:
            device = Device(mac=body.mac, hw=body.hw, app=body.app, fw_version=body.fw_version)
            session.add(device)

        device.hw = body.hw
        device.app = body.app
        device.fw_version = body.fw_version
        device.ip = body.ip or (request.client.host if request.client else None)
        device.rssi = body.rssi
        device.uptime_s = body.uptime_s
        device.last_seen = utcnow()

        order = None
        if deployment := device.active_deployment:
            fw = deployment.firmware
            if body.app == fw.app and body.fw_version == fw.version:
                deployment.status, deployment.progress = "success", 100
            elif deployment.status == "rebooting":
                deployment.status = "failed"
                deployment.error = f"came back running {body.fw_version} (rollback?)"
            else:
                # pending, or an interrupted download: (re)send the order.
                base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
                order = UpdateOrder(
                    deployment_id=deployment.id,
                    version=fw.version,
                    url=f"{base}/api/v1/firmwares/{fw.id}/download",
                    size=fw.size,
                    sha256=fw.sha256,
                )

        session.commit()
        publish_device(device)
        return order


@router.post("/deployments/{deployment_id}/progress")
def report_progress(deployment_id: int, body: ProgressIn, session: Session = Depends(get_session)):
    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(404, "unknown deployment")
    if deployment.status not in ("pending", "downloading", "rebooting"):
        # Cancelled or already finished: tells the device to abort.
        raise HTTPException(409, f"deployment is {deployment.status}")

    deployment.status = body.state
    deployment.progress = 100 if body.state == "rebooting" else body.progress
    deployment.error = body.error if body.state == "failed" else None
    deployment.device.last_seen = utcnow()
    session.commit()
    publish_device(deployment.device)
    return {"ok": True}


@router.get("/firmwares/{firmware_id}/download")
def download_firmware(firmware_id: int, session: Session = Depends(get_session)):
    fw = session.get(Firmware, firmware_id)
    if fw is None:
        raise HTTPException(404, "unknown firmware")
    return FileResponse(
        firmware_path(fw.sha256),
        media_type="application/octet-stream",
        filename=f"{fw.app}-{fw.hw}-{fw.version}.bin",
    )
