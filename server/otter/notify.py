"""Outgoing notifications: ntfy, Discord, or generic JSON webhooks.

Targets come from OTTER_NOTIFY_URLS (space or comma separated):
    ntfy+https://ntfy.sh/my-topic           ntfy (user:password@ in the URL for basic auth)
    https://discord.com/api/webhooks/…      Discord, detected from the URL
    https://example.com/hook                anything else: generic JSON

Events are queued and sent from a background thread, so a slow or broken webhook
never delays a check-in. Only ids travel through the queue: messages are built from
the database when sent.
"""

import base64
import json
import logging
import queue
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from . import config
from .db import SessionLocal, utcnow
from .models import Deployment, Device, Rollout, silence_allowed

log = logging.getLogger("otter.notify")

INFO, WARNING, ERROR = "info", "warning", "error"
NTFY_PRIORITY = {INFO: 2, WARNING: 3, ERROR: 4}
NTFY_TAGS = {
    "deployment_failed": ["warning"],
    "rollout_halted": ["rotating_light"],
    "device_offline": ["zzz"],
    "device_online": ["white_check_mark"],
    "device_new": ["new"],
    "device_crashed": ["boom"],
    "test": ["test_tube"],
}
DISCORD_COLOR = {INFO: 0x2F6FDF, WARNING: 0xC98A00, ERROR: 0xD64545}


@dataclass
class Message:
    event: str
    title: str
    body: str
    severity: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Target:
    kind: str  # ntfy, discord, webhook
    url: str

    def describe(self) -> str:
        """The URL without credentials, path or secrets, safe to show in the UI or logs."""
        parts = urlsplit(self.url)
        return f"{self.kind} {parts.hostname}"


def parse_targets(spec: str) -> list[Target]:
    targets = []
    for item in spec.replace(",", " ").split():
        if item.startswith("ntfy+"):
            targets.append(Target("ntfy", item.removeprefix("ntfy+")))
        elif urlsplit(item).hostname in ("discord.com", "discordapp.com") and "/api/webhooks/" in item:
            targets.append(Target("discord", item))
        else:
            targets.append(Target("webhook", item))
    return targets


# --- Rendering --------------------------------------------------------------------


def render(target: Target, msg: Message) -> tuple[str, dict[str, str], dict]:
    """(url, headers, JSON payload) for one target."""
    headers = {"Content-Type": "application/json", "User-Agent": "Otter"}
    link = config.PUBLIC_URL or None
    if target.kind == "ntfy":
        # JSON publishing: the topic goes in the body, which also allows non-ASCII titles.
        parts = urlsplit(target.url)
        base_path, _, topic = parts.path.rstrip("/").rpartition("/")
        if parts.username:
            creds = f"{parts.username}:{parts.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        url = urlunsplit((parts.scheme, netloc, base_path or "/", "", ""))
        payload = {
            "topic": topic,
            "title": msg.title,
            "message": msg.body,
            "priority": NTFY_PRIORITY[msg.severity],
            "tags": NTFY_TAGS.get(msg.event, []),
        }
        if link:
            payload["click"] = link
        return url, headers, payload
    if target.kind == "discord":
        embed = {"title": msg.title, "description": msg.body, "color": DISCORD_COLOR[msg.severity]}
        if link:
            embed["url"] = link
        return target.url, headers, {"username": "Otter", "embeds": [embed]}
    return target.url, headers, {
        "event": msg.event,
        "title": msg.title,
        "message": msg.body,
        "severity": msg.severity,
        "data": msg.data,
    }


def post(url: str, headers: dict[str, str], payload: dict) -> None:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


# --- Messages --------------------------------------------------------------------


def label(device: Device) -> str:
    return device.name or device.mac


def build(event: str, ids: dict) -> Message | None:
    """Builds the message from the database; None if its subject disappeared meanwhile."""
    with SessionLocal() as session:
        if event == "deployment_failed":
            dep = session.get(Deployment, ids["deployment_id"])
            if dep is None:
                return None
            fw, device = dep.firmware, dep.device
            body = f"{fw.app} {fw.version}: {dep.error or 'unknown error'}"
            if dep.rollout_id is not None:
                body += f" (rollout #{dep.rollout_id}, stage {dep.stage + 1})"
            data = {"device": device.mac, "app": fw.app, "version": fw.version, "error": dep.error}
            return Message(event, f"Update failed on {label(device)}", body, ERROR, data)

        if event == "rollout_halted":
            rollout = session.get(Rollout, ids["rollout_id"])
            if rollout is None:
                return None
            fw = rollout.firmware
            body = (
                f"{rollout.message}. The remaining devices are on hold: resume with the next stage "
                "or abort the rollout from Otter."
            )
            data = {"rollout": rollout.id, "app": fw.app, "version": fw.version, "reason": rollout.message}
            return Message(event, f"Rollout halted: {fw.app} {fw.version}", body, ERROR, data)

        device = session.get(Device, ids["device_id"])
        if device is None:
            return None
        data = {"device": device.mac, "app": device.app, "version": device.fw_version, "ip": device.ip}
        where = f"{device.app} {device.fw_version} on {device.hw}, {device.ip or 'no IP'}"
        if event == "device_offline":
            minutes = round((utcnow() - device.last_seen).total_seconds() / 60)
            return Message(event, f"{label(device)} is offline", f"No check-in for {minutes} min ({where}).", WARNING, data)
        if event == "device_online":
            return Message(event, f"{label(device)} is back online", where, INFO, data)
        if event == "device_new":
            return Message(event, f"New device: {label(device)}", where, INFO, data)
        if event == "device_crashed":
            data["reset_reason"] = ids["reason"]  # the device may have restarted again since
            reason = ids["reason"].replace("_", " ")
            return Message(event, f"{label(device)} restarted after a {reason}", where, WARNING, data)
    raise ValueError(f"unknown event {event}")


# --- Delivery ----------------------------------------------------------------------


class Notifier:
    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[str, dict] | None] = queue.Queue(maxsize=1000)
        self.post = post  # replaced in tests
        self._thread: threading.Thread | None = None
        self._offline: set[int] = set()  # devices we sent an "offline" alert for
        self._lock = threading.Lock()

    def targets(self) -> list[Target]:
        return parse_targets(config.NOTIFY_URLS)

    def emit(self, event: str, **ids) -> None:
        if not self.targets():
            return
        try:
            self.queue.put_nowait((event, ids))
        except queue.Full:
            log.warning("notification queue full, dropping %s", event)

    def send(self, msg: Message) -> list[dict]:
        results = []
        for target in self.targets():
            try:
                self.post(*render(target, msg))
                results.append({"target": target.describe(), "ok": True, "error": None})
            except Exception as exc:  # a broken target must not affect the others
                log.warning("notification to %s failed: %s", target.describe(), exc)
                results.append({"target": target.describe(), "ok": False, "error": str(exc)})
        return results

    def process(self, event: str, ids: dict) -> None:
        try:
            if msg := build(event, ids):
                self.send(msg)
        except Exception:
            log.exception("could not build %s notification", event)

    def drain(self) -> None:
        """Processes queued events synchronously (tests)."""
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return
            if item:
                self.process(*item)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="otter-notify", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self.queue.put(None)
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while (item := self.queue.get()) is not None:
            self.process(*item)

    # Offline tracking ------------------------------------------------------------

    def seed_offline(self, now: datetime | None = None) -> None:
        """Devices already offline at startup don't trigger alerts."""
        with SessionLocal() as session, self._lock:
            self._offline = self._offline_ids(session, now)

    def check_offline(self, now: datetime | None = None) -> None:
        if not self.targets() or config.NOTIFY_OFFLINE_MINUTES <= 0:
            return
        with SessionLocal() as session:
            offline = self._offline_ids(session, now)
        with self._lock:
            new = offline - self._offline
            self._offline = (self._offline & offline) | new  # forget devices seen again or deleted
        for device_id in sorted(new):
            self.emit("device_offline", device_id=device_id)

    def device_seen(self, device_id: int) -> None:
        with self._lock:
            was_offline = device_id in self._offline
            self._offline.discard(device_id)
        if was_offline:
            self.emit("device_online", device_id=device_id)

    @staticmethod
    def _offline_ids(session, now: datetime | None) -> set[int]:
        """Devices silent for longer than allowed (longer for those that said they'd sleep)."""
        now = now or utcnow()
        base = config.NOTIFY_OFFLINE_MINUTES * 60
        rows = session.execute(select(Device.id, Device.last_seen, Device.next_checkin_s))
        return {i for i, seen, sleep in rows if (now - seen).total_seconds() > silence_allowed(base, sleep)}


notifier = Notifier()
