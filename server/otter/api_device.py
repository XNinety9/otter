"""Endpoints called by devices. See docs/protocol.md."""

import asyncio
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import commands, config
from .auth import token_hash
from .db import SessionLocal, get_session, utcnow
from .device_auth import DeviceAuth, authenticate, check_access, new_token
from .events import broadcaster, wakeups
from .metrics import CHECKINS, CRASHES, DOWNLOAD_BYTES, DOWNLOADS
from .notify import notifier
from .models import CRASH_RESETS, Command, Deployment, Device, Firmware
from .schemas import CheckinIn, CheckinOut, CommandOrder, CommandResultIn, DeviceOut, ProgressIn, UpdateOrder
from .storage import firmware_path


# Longest a check-in may be held open (long polling).
MAX_WAIT_S = 60

# Failures a new attempt may fix, as reported by the agents (ESP-IDF, Arduino, simulator).
TRANSIENT_ERRORS = ("connection lost", "download timeout", "cannot reach", "download refused", "out of memory")


def is_transient(error: str | None, retryable: bool | None) -> bool:
    if retryable is not None:
        return retryable
    return any(marker in (error or "").lower() for marker in TRANSIENT_ERRORS)


router = APIRouter(prefix="/api/v1", tags=["device"])


def publish_device(device: Device) -> None:
    broadcaster.publish("device", DeviceOut.model_validate(device).model_dump(mode="json"))


@router.post("/checkin", response_model=CheckinOut)
async def checkin(body: CheckinIn, request: Request, auth: DeviceAuth = Depends(authenticate)):
    with wakeups.watch(body.mac) as scheduled:
        order, orders, token = await run_in_threadpool(record_checkin, body, request, auth)
        if order is None and not orders and body.wait_s:
            try:
                await asyncio.wait_for(scheduled.wait(), min(body.wait_s, MAX_WAIT_S))
                # Something was scheduled while we waited: re-evaluate to build the orders.
                order, orders, token = await run_in_threadpool(record_checkin, body, request, auth)
            except TimeoutError:
                pass
    return CheckinOut(checkin_interval_s=config.CHECKIN_INTERVAL_S, update=order, commands=orders, token=token)


def record_checkin(
    body: CheckinIn, request: Request, auth: DeviceAuth
) -> tuple[UpdateOrder | None, list[CommandOrder], str | None]:
    with SessionLocal() as session:
        device = session.scalar(select(Device).where(Device.mac == body.mac))
        is_new = device is None
        if is_new:
            if auth.device_id is not None:
                raise HTTPException(403, "this token belongs to another device")
            device = Device(mac=body.mac, hw=body.hw, app=body.app, fw_version=body.fw_version)
            device.approved = not config.DEVICE_APPROVAL
            session.add(device)
        else:
            check_access(auth, device)

        device.hw = body.hw
        device.app = body.app
        device.fw_version = body.fw_version
        rebooted = not is_new and restarted(device, body)
        device.ip = body.ip or (request.client.host if request.client else None)
        device.rssi = body.rssi
        device.uptime_s = body.uptime_s
        device.free_heap, device.min_free_heap = body.free_heap, body.min_free_heap
        device.reset_reason, device.boot_count = body.reset_reason, body.boot_count
        if body.ota_slot_size is not None:
            device.ota_slot_size = body.ota_slot_size
        crashed = rebooted and body.reset_reason in CRASH_RESETS
        device.last_seen = utcnow()
        CHECKINS.labels(body.app).inc()

        if not device.approved:
            session.commit()
            publish_device(device)
            if is_new:
                notifier.emit("device_new", device_id=device.id)
            raise HTTPException(403, "device awaiting approval in Otter")
        # Until the device uses a token, each fleet-key check-in gets a fresh one.
        token = None
        if auth.fleet and device.token_used_at is None:
            token = new_token()
            device.token_hash = token_hash(token)

        order = None
        failed = None
        if deployment := device.active_deployment:
            fw = deployment.firmware
            if body.app == fw.app and body.fw_version == fw.version:
                deployment.status, deployment.progress = "success", 100
            elif deployment.status == "rebooting":
                deployment.status = "failed"
                deployment.error = f"came back running {body.fw_version} (rollback?)"
                failed = deployment
            elif deployment.retry_at and deployment.retry_at > utcnow():
                pass  # a retry, not due yet
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

        sent, expired = commands.deliver(device)
        session.commit()
        publish_device(device)
        for command in sent + expired:
            commands.publish(command)
        if is_new:
            notifier.emit("device_new", device_id=device.id)
        else:
            notifier.device_seen(device.id)
        if failed:
            notifier.emit("deployment_failed", deployment_id=failed.id)
        if crashed:
            CRASHES.labels(body.app, body.reset_reason).inc()
            notifier.emit("device_crashed", device_id=device.id, reason=body.reset_reason)
        return order, commands.as_orders(sent), token


def restarted(device: Device, body: CheckinIn) -> bool:
    """Whether the device rebooted since its previous check-in."""
    if body.boot_count is not None and device.boot_count is not None:
        return body.boot_count != device.boot_count
    return body.uptime_s is not None and device.uptime_s is not None and body.uptime_s < device.uptime_s


@router.post("/commands/{command_id}/result")
def command_result(
    command_id: int,
    body: CommandResultIn,
    auth: DeviceAuth = Depends(authenticate),
    session: Session = Depends(get_session),
):
    command = session.get(Command, command_id)
    if command is None:
        raise HTTPException(404, "unknown command")
    check_access(auth, command.device)
    commands.record_result(command, body.ok, body.message)
    session.commit()
    commands.publish(command)
    return {"ok": True}


@router.post("/deployments/{deployment_id}/progress")
def report_progress(
    deployment_id: int,
    body: ProgressIn,
    auth: DeviceAuth = Depends(authenticate),
    session: Session = Depends(get_session),
):
    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(404, "unknown deployment")
    check_access(auth, deployment.device)
    if deployment.status not in ("pending", "downloading", "rebooting"):
        # Cancelled or already finished: tells the device to abort.
        raise HTTPException(409, f"deployment is {deployment.status}")

    if body.state == "failed" and deployment.attempts < config.DEPLOY_ATTEMPTS and is_transient(
        body.error, body.retryable
    ):
        # Hand it out again a bit later: the device gets it back at a later check-in.
        deployment.status, deployment.progress = "pending", 0
        deployment.error = f"attempt {deployment.attempts} failed: {body.error or 'unknown error'}"
        deployment.retry_at = utcnow() + timedelta(seconds=config.RETRY_DELAY_S * deployment.attempts)
        deployment.attempts += 1
    elif body.state == "failed":
        deployment.status, deployment.progress = "failed", 0
        suffix = f" (attempt {deployment.attempts}/{config.DEPLOY_ATTEMPTS})" if deployment.attempts > 1 else ""
        deployment.error = (body.error or "unknown error") + suffix
    else:
        deployment.status = body.state
        deployment.progress = 100 if body.state == "rebooting" else body.progress
        deployment.error = None
    deployment.device.last_seen = utcnow()
    session.commit()
    publish_device(deployment.device)
    notifier.device_seen(deployment.device_id)
    if deployment.status == "failed":  # final failures only, not the ones being retried
        notifier.emit("deployment_failed", deployment_id=deployment.id)
    return {"ok": True}


@router.get("/firmwares/{firmware_id}/download")
def download_firmware(
    firmware_id: int, request: Request, auth: DeviceAuth = Depends(authenticate), session: Session = Depends(get_session)
):
    """The whole image, or its end with `Range: bytes=<offset>-` when a device resumes a stalled download."""
    fw = session.get(Firmware, firmware_id)
    if fw is None:
        raise HTTPException(404, "unknown firmware")
    resume = re.fullmatch(r"bytes=(\d+)-", request.headers.get("range", ""))
    offset = min(int(resume.group(1)), fw.size) if resume else 0
    if offset == 0:
        DOWNLOADS.labels(fw.app, fw.version).inc()
    DOWNLOAD_BYTES.inc(fw.size - offset)
    return FileResponse(
        firmware_path(fw.sha256),
        media_type="application/octet-stream",
        filename=f"{fw.app}-{fw.hw}-{fw.version}.bin",
    )
