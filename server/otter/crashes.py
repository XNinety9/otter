"""Crash reports (#24): match a core dump summary with its firmware and decode its call stack."""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Crash, Device, Firmware
from .schemas import CrashIn
from .symbols import symbolizer

log = logging.getLogger("otter.crashes")

MAX_STACK_FRAMES = 12  # code addresses picked from a RISC-V stack dump


def find_firmware(session: Session, device: Device, elf_sha256: str) -> Firmware | None:
    """The image the crashed app was built from: the summary names its ELF (a prefix of its hash)."""
    return session.scalar(
        select(Firmware)
        .where(Firmware.hw == device.hw, Firmware.elf_sha256.startswith(elf_sha256))
        .order_by(Firmware.has_elf.desc(), Firmware.id.desc())
    )


def frames(report: dict, elf_sha256: str | None) -> list[dict]:
    """The call stack: exact on Xtensa (backtrace); on RISC-V, the PC, the return address, then
    code addresses found on the stack (probable callers, the summary has no unwinding)."""
    sym = None
    if elf_sha256:
        try:
            sym = symbolizer(elf_sha256)
        except Exception:
            log.exception("can't read the ELF file %s", elf_sha256)

    def frame(address: int, kind: str) -> dict:
        return sym.frame(address, kind) if sym else {"address": f"0x{address:08x}", "kind": kind}

    if report.get("backtrace"):
        return [frame(a, "pc" if i == 0 else "return") for i, a in enumerate(report["backtrace"])]
    result = [frame(report["pc"], "pc")]
    ra = report.get("registers", {}).get("ra")
    if ra:
        result.append(frame(ra, "return"))
    if sym:
        seen = {report["pc"], ra}
        for word in report.get("stack", []):
            if word not in seen and sym.is_code(word):
                seen.add(word)
                result.append(frame(word, "stack"))
                if len(result) >= MAX_STACK_FRAMES + 2:
                    break
    return result


def record(session: Session, device: Device, body: CrashIn) -> Crash:
    firmware = find_firmware(session, device, body.elf_sha256)
    report = body.model_dump()
    crash = Crash(
        device=device,
        firmware=firmware,
        elf_sha256=body.elf_sha256,
        fw_version=firmware.version if firmware else device.fw_version,
        task=body.task,
        reason=body.reason,
        report=json.dumps(report),
        frames=json.dumps(frames(report, firmware.elf_sha256 if firmware and firmware.has_elf else None)),
    )
    session.add(crash)
    return crash


def adopt(session: Session, firmware: Firmware) -> None:
    """Crashes reported before their firmware was uploaded (e.g. flashed over USB) join it."""
    if not firmware.elf_sha256:
        return
    orphans = session.scalars(
        select(Crash).join(Device).where(Crash.firmware_id.is_(None), Device.hw == firmware.hw)
    ).all()
    for crash in orphans:
        if firmware.elf_sha256.startswith(crash.elf_sha256):
            crash.firmware = firmware
            crash.fw_version = firmware.version
    if firmware.has_elf:
        decode_again(session, firmware)


def decode_again(session: Session, firmware: Firmware) -> None:
    """Names the frames of earlier crashes of this firmware, once its ELF file is uploaded."""
    for crash in session.scalars(select(Crash).where(Crash.firmware_id == firmware.id)):
        crash.frames = json.dumps(frames(json.loads(crash.report), firmware.elf_sha256))
