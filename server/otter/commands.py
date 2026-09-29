"""Remote commands: queued from the UI, delivered in check-in responses (at once to a device
that long-polls), then acknowledged by the device. See "Commands" in docs/protocol.md."""

import json
from datetime import timedelta

from .db import utcnow
from .events import broadcaster
from .models import Command, Device
from .schemas import CommandOrder, CommandOut

# A command still queued after this long is dropped rather than run at an unexpected time
# (a reboot asked for yesterday shouldn't happen today).
TTL = timedelta(minutes=10)
PER_CHECKIN = 5  # the rest follows at the next check-in, right away with long polling


def publish(command: Command) -> None:
    broadcaster.publish("command", CommandOut.model_validate(command).model_dump(mode="json"))


def queue(device: Device, name: str, args: dict | None) -> Command:
    command = Command(name=name, args=json.dumps(args) if args is not None else None)
    device.commands.append(command)
    return command


def deliver(device: Device) -> tuple[list[Command], list[Command]]:
    """(commands to send with this check-in, now marked as sent; commands that just expired)."""
    now = utcnow()
    due, expired = [], []
    for command in device.commands:
        if command.status != "queued":
            continue
        if now - command.created_at > TTL:
            command.status, command.result, command.done_at = "expired", "not delivered within 10 min", now
            expired.append(command)
        elif len(due) < PER_CHECKIN:
            command.status, command.sent_at = "sent", now
            due.append(command)
    return due, expired


def as_orders(commands: list[Command]) -> list[CommandOrder]:
    return [CommandOrder(id=c.id, name=c.name, args=json.loads(c.args) if c.args else None) for c in commands]


def record_result(command: Command, ok: bool, message: str | None) -> None:
    command.status = "done" if ok else "failed"
    command.result = message
    command.done_at = utcnow()
