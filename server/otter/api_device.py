"""Endpoints called by devices. See docs/protocol.md."""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import commands, config, crashes, devconfig
from .auth import token_hash
from .db import SessionLocal, get_session, utcnow
from .device_auth import DeviceAuth, authenticate, check_access, new_token
from .events import broadcaster, wakeups
from .metrics import CHECKINS, CRASHES, DOWNLOAD_BYTES, DOWNLOADS
from .logs import device_logs
from .notify import notifier
from .models import CRASH_RESETS, Command, Deployment, Device, Firmware
from .schemas import (
    CheckinIn, CheckinOut, CommandResultIn, CompressedImage, ConfigOrder, CrashIn, DeltaPatch, DeviceOut, LogsIn, ProgressIn,
    UpdateOrder,
)
from .storage import compressed_path, delta_path, delta_size, firmware_path, image_hash


# Longest a check-in may be held open (long polling).
MAX_WAIT_S = 60

# Failures a new attempt may fix, as reported by the agents (ESP-IDF, Arduino, simulator).
TRANSIENT_ERRORS = (
    "connection lost", "download timeout", "cannot reach", "download refused", "out of memory", "delta patch failed",
)


def is_transient(error: str | None, retryable: bool | None) -> bool:
    if retryable is not None:
        return retryable
    return any(marker in (error or "").lower() for marker in TRANSIENT_ERRORS)


log = logging.getLogger("otter.devices")

router = APIRouter(prefix="/api/v1", tags=["device"])


def publish_device(device: Device) -> None:
    broadcaster.publish("device", DeviceOut.model_validate(device).model_dump(mode="json"))


@router.post("/checkin", response_model=CheckinOut)
async def checkin(body: CheckinIn, request: Request, auth: DeviceAuth = Depends(authenticate)):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(body.wait_s, MAX_WAIT_S)
    with wakeups.watch(body.mac) as scheduled:
        answer, seen = await run_in_threadpool(record_checkin, body, request, auth)
        while not answer.has_news() and (remaining := deadline - loop.time()) > 0:
            try:
                await asyncio.wait_for(scheduled.wait(), remaining)
            except TimeoutError:
                break
            # Something may concern the device: re-evaluate. A wake-up can also bring nothing
            # for it (e.g. a tag without configuration): then keep waiting, or the device would
            # sleep a whole interval and miss what comes next.
            scheduled.clear()
            answer, seen = await run_in_threadpool(record_checkin, body, request, auth, seen)
            if seen is None:
                # The device checked in again since (it rebooted, or gave up on this request):
                # nobody listens here, so deliver nothing and don't overwrite its newer state.
                break
    return answer


@router.post("/logs", status_code=204)
def receive_logs(body: LogsIn, auth: DeviceAuth = Depends(authenticate)):
    """Log lines from a device asked to send them (see logs.py)."""
    with SessionLocal() as session:
        device = session.scalar(select(Device).where(Device.mac == body.mac)) or _unknown_device()
        check_access(auth, device)
        device_id = device.id
    device_logs.add(device_id, [line.rstrip() for line in body.lines])


def _unknown_device():
    raise HTTPException(404, "unknown device: check in first")


def token_lost(session, device: Device) -> None:
    """A device with its own token checked in with the fleet key only: refused, but shown."""
    first = device.token_lost_at is None
    # Devices retry every few seconds: write it, and tell the UI, at most once a minute.
    if first or utcnow() - device.token_lost_at > timedelta(minutes=1):
        device.token_lost_at = utcnow()
        session.commit()
        publish_device(device)
    if first:
        notifier.emit("device_token_lost", device_id=device.id)


def record_checkin(
    body: CheckinIn, request: Request, auth: DeviceAuth, seen: datetime | None = None
) -> tuple[CheckinOut, datetime | None]:
    """Records a check-in and builds the answer, with the last_seen it wrote. On a long poll's
    re-evaluation, seen is what the previous evaluation wrote: if it changed, a newer check-in
    superseded this one and (empty answer, None) comes back."""
    with SessionLocal() as session:
        device = session.scalar(select(Device).where(Device.mac == body.mac))
        if seen is not None and (device is None or device.last_seen != seen):
            return CheckinOut(checkin_interval_s=config.CHECKIN_INTERVAL_S), None
        is_new = device is None
        if is_new:
            if auth.device_id is not None:
                raise HTTPException(403, "this token belongs to another device")
            device = Device(mac=body.mac, hw=body.hw, app=body.app, fw_version=body.fw_version)
            device.approved = not config.DEVICE_APPROVAL
            session.add(device)
        else:
            try:
                check_access(auth, device)
            except HTTPException as exc:
                if exc.status_code == 401 and device.token_used_at is not None:
                    token_lost(session, device)
                raise

        device.hw = body.hw
        if body.chip:  # agents from before #74 don't send it
            device.chip, device.chip_rev = body.chip, body.chip_rev
            device.flash_size, device.psram_size = body.flash_size, body.psram_size
            device.radio = body.radio
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

        device.config_version = body.config_version
        device.next_checkin_s = body.next_checkin_s
        config_order = None
        if body.config_version is not None:
            values, _ = devconfig.effective(session, device)
            if body.config_version != (current := devconfig.version(values)):
                config_order = ConfigOrder(version=current, values=values)

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
                    signature=fw.signature,
                    compressed=CompressedImage(
                        url=f"{base}/api/v1/firmwares/{fw.id}/download?format=zlib", size=fw.compressed_size
                    ) if fw.compressed_size else None,
                    delta=delta_for(session, device, deployment, fw, base),
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
        answer = CheckinOut(
            checkin_interval_s=config.CHECKIN_INTERVAL_S,
            update=order,
            commands=commands.as_orders(sent),
            token=token,
            config=config_order,
            logs_s=device_logs.order(device.id) if body.logs else None,
        )
        if answer.logs_s is not None:
            device_logs.told(device.id)
        return answer, device.last_seen


# A patch is offered when well below the compressed image, and at the first attempt only: a
# retry after a failed patch downloads the whole image.
DELTA_MAX_RATIO = 0.5


def delta_for(session: Session, device: Device, deployment: Deployment, fw: Firmware, base: str) -> DeltaPatch | None:
    """A patch from the image the device runs, if Otter has it (#26). The device checks that it
    really runs that image (base_hash) before using the patch."""
    if deployment.attempts > 1:
        return None
    current = session.scalar(
        select(Firmware).where(Firmware.app == device.app, Firmware.hw == device.hw, Firmware.version == device.fw_version)
    )
    if current is None or current.id == fw.id:
        return None
    try:
        base_hash = image_hash(current.sha256)
        if not base_hash or not image_hash(fw.sha256):
            return None
        size = delta_size(current.sha256, fw.sha256)
    except Exception:
        log.exception("can't make a patch from %s to %s", current.version, fw.version)
        return None
    if size > (fw.compressed_size or fw.size) * DELTA_MAX_RATIO:
        return None
    return DeltaPatch(url=f"{base}/api/v1/firmwares/{fw.id}/delta?base={current.id}", size=size, base_hash=base_hash)


def restarted(device: Device, body: CheckinIn) -> bool:
    """Whether the device rebooted since its previous check-in."""
    if body.boot_count is not None and device.boot_count is not None:
        return body.boot_count != device.boot_count
    return body.uptime_s is not None and device.uptime_s is not None and body.uptime_s < device.uptime_s


@router.post("/crashes")
def report_crash(
    body: CrashIn,
    mac: str | None = None,
    auth: DeviceAuth = Depends(authenticate),
    session: Session = Depends(get_session),
):
    """A core dump summary, sent at the first check-in after a crash (#24). A device using the
    fleet key names itself with ?mac=."""
    if auth.device_id is not None:
        device = session.get(Device, auth.device_id)
    else:
        try:
            normalized = CheckinIn.normalize_mac(mac or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        device = session.scalar(select(Device).where(Device.mac == normalized))
        if device is None:
            raise HTTPException(404, "unknown device")
        check_access(auth, device)
    crash = crashes.record(session, device, body)
    session.commit()
    broadcaster.publish("crash", {"device_id": device.id, "id": crash.id})
    return {"ok": True, "decoded": crash.decoded}


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


@router.get("/firmwares/{firmware_id}/delta")
def download_delta(
    firmware_id: int,
    base: int,
    request: Request,
    auth: DeviceAuth = Depends(authenticate),
    session: Session = Depends(get_session),
):
    """The patch from firmware `base` to this one (#26); ranges work as for images."""
    fw, base_fw = session.get(Firmware, firmware_id), session.get(Firmware, base)
    if fw is None or base_fw is None:
        raise HTTPException(404, "unknown firmware")
    size = delta_size(base_fw.sha256, fw.sha256)
    resume = re.fullmatch(r"bytes=(\d+)-", request.headers.get("range", ""))
    offset = min(int(resume.group(1)), size) if resume else 0
    if offset == 0:
        DOWNLOADS.labels(fw.app, fw.version).inc()
    DOWNLOAD_BYTES.inc(size - offset)
    return FileResponse(
        delta_path(base_fw.sha256, fw.sha256),
        media_type="application/octet-stream",
        filename=f"{fw.app}-{fw.hw}-{base_fw.version}-to-{fw.version}.patch",
    )


@router.get("/firmwares/{firmware_id}/download")
def download_firmware(
    firmware_id: int,
    request: Request,
    format: Literal["bin", "zlib"] = "bin",
    auth: DeviceAuth = Depends(authenticate),
    session: Session = Depends(get_session),
):
    """The whole image, or its end with `Range: bytes=<offset>-` when a device resumes a stalled
    download. format=zlib: its compressed copy (#25), ranges then count compressed bytes."""
    fw = session.get(Firmware, firmware_id)
    if fw is None:
        raise HTTPException(404, "unknown firmware")
    if format == "zlib" and not fw.compressed_size:
        raise HTTPException(404, "no compressed copy of this image")
    path, size = (compressed_path(fw.sha256), fw.compressed_size) if format == "zlib" else (firmware_path(fw.sha256), fw.size)
    resume = re.fullmatch(r"bytes=(\d+)-", request.headers.get("range", ""))
    offset = min(int(resume.group(1)), size) if resume else 0
    if offset == 0:
        DOWNLOADS.labels(fw.app, fw.version).inc()
    DOWNLOAD_BYTES.inc(size - offset)
    return FileResponse(
        path, media_type="application/octet-stream", filename=f"{fw.app}-{fw.hw}-{fw.version}.{format}"
    )
