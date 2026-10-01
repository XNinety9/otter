#!/usr/bin/env python3
"""Simulate a swarm of Otter devices, following docs/protocol.md.

    uv run --with httpx tools/simulate.py --count 8 --fail-rate 0.1

Each fake device checks in periodically; when given an update it downloads the image
(slowly, to watch progress bars), checks its SHA-256, "reboots" and comes back on the
new version -- or rolls back, depending on --fail-rate.

Like real devices, fake ones keep the token they get when enrolling: in --tokens (default
~/.cache/otter-simulator.json), so a later run can still check in as them.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import ssl

import httpx

APPS = [("weather-station", "esp32"), ("plant-sensor", "esp8266"), ("led-strip", "esp32c3")]
# OTA slot sizes of common layouts: ESP-IDF's two-OTA default, a 4 MB ESP8266, 1.9 MB slots.
SLOT_SIZES = {"esp32": 0x140000, "esp8266": 0xFB000, "esp32c3": 0x1E0000}
# Exact chip and revision, as the agents report them.
CHIPS = {"esp32": ("ESP32-D0WD-V3", "3.1"), "esp8266": ("ESP8266EX", None), "esp32c3": ("ESP32-C3 (QFN32)", "0.4")}
FLASH_SIZES = {"esp32": 4 << 20, "esp8266": 1 << 20, "esp32c3": 4 << 20}
RADIOS = {"esp32": "Wi-Fi 4, Bluetooth 4.2 (Classic + LE)", "esp8266": "Wi-Fi 4", "esp32c3": "Wi-Fi 4, Bluetooth 5 (LE)"}


TOKENS: dict[str, str] = {}


def save_token(args: argparse.Namespace, mac: str, token: str | None) -> None:
    key = f"{args.server} {mac}"
    if token:
        TOKENS[key] = token
    else:
        TOKENS.pop(key, None)
    path = Path(args.tokens)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(TOKENS, indent=1))
    os.chmod(path, 0o600)


class FakeDevice:
    def __init__(self, n: int, args: argparse.Namespace) -> None:
        self.args = args
        self.mac = f"de:ad:be:ef:{n // 256:02x}:{n % 256:02x}"
        self.app, self.hw = APPS[n % len(APPS)]
        self.version = "1.0.0"
        self.ip = f"192.168.1.{100 + n}"
        self.boot = asyncio.get_running_loop().time()
        self.boots = 1
        self.reset_reason = "power_on"
        # Its own token once enrolled (see "Authentication" in docs/protocol.md), kept across runs.
        self.token = TOKENS.get(f"{args.server} {self.mac}")
        self.config_version, self.config = "", {}
        self.interval = args.interval or 30

    @property
    def headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {"X-Otter-Key": self.args.key} if self.args.key else {}

    def log(self, msg: str) -> None:
        print(f"[{self.mac}] {msg}", flush=True)

    async def run(self, client: httpx.AsyncClient) -> None:
        await asyncio.sleep(random.uniform(0, 2))
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                resp = await self.checkin(client)
                if config := resp.get("config"):
                    self.config_version, self.config = config["version"], config["values"]
                    self.log(f"configuration: {self.config}")
                    continue  # confirm it right away
                if commands := resp.get("commands"):
                    await self.run_commands(client, commands)
                    continue  # more may be right behind
                if order := resp["update"]:
                    await self.apply(client, order)
                    continue  # "reboot": check in again right away
                self.interval = self.args.interval or resp["checkin_interval_s"]
                # With long polling the server already held us: poll again right away.
                delay = self.interval * random.uniform(0.9, 1.1) - (loop.time() - started)
            except httpx.HTTPError as exc:
                self.log(f"server unreachable: {exc!r}")
                delay = 5
            await asyncio.sleep(max(delay, 0))

    async def run_commands(self, client: httpx.AsyncClient, commands: list[dict]) -> None:
        reboot = False
        for command in commands:
            name, args = command["name"], command.get("args") or {}
            if name == "reboot":
                ok, message, reboot = True, "rebooting", True
            elif name == "identify":
                ok, message = True, "blinked the (imaginary) LED"
            elif name == "echo":
                ok, message = True, json.dumps(args)
            else:
                ok, message = False, "unknown command"
            self.log(f"command {name} {args or ''}-> {message}")
            await client.post(
                f"/api/v1/commands/{command['id']}/result", headers=self.headers, json={"ok": ok, "message": message}
            )
        if reboot:
            await self.restart("software")

    async def restart(self, reason: str) -> None:
        await asyncio.sleep(random.uniform(2, 4))
        self.boot = asyncio.get_running_loop().time()
        self.boots += 1
        self.reset_reason = reason

    async def checkin(self, client: httpx.AsyncClient) -> dict:
        loop = asyncio.get_running_loop()
        res = await client.post(
            "/api/v1/checkin",
            headers=self.headers,
            json={
                "mac": self.mac,
                "hw": self.hw,
                "chip": CHIPS[self.hw][0],
                "chip_rev": CHIPS[self.hw][1],
                "flash_size": FLASH_SIZES[self.hw],
                "radio": RADIOS[self.hw],
                "app": self.app,
                "fw_version": self.version,
                "ip": self.ip,
                "rssi": random.randint(-85, -45),
                "uptime_s": int(loop.time() - self.boot),
                "ota_slot_size": SLOT_SIZES[self.hw],
                "reset_reason": self.reset_reason,
                "boot_count": self.boots,
                "config_version": self.config_version,
                "wait_s": 0 if self.args.no_long_poll else int(self.interval),
            },
            timeout=self.interval + 15,
        )
        if res.status_code == 401 and self.token:
            self.log("token refused, enrolling again with the fleet key")
            self.token = None
            save_token(self.args, self.mac, None)
        res.raise_for_status()
        data = res.json()
        if data.get("token"):
            self.token = data["token"]
            save_token(self.args, self.mac, self.token)
        return data

    async def report(self, client: httpx.AsyncClient, dep: int, state: str, progress=0, error=None) -> bool:
        res = await client.post(
            f"/api/v1/deployments/{dep}/progress",
            headers=self.headers,
            json={"state": state, "progress": progress, "error": error},
        )
        return res.status_code != 409  # 409 = cancelled, abort

    async def apply(self, client: httpx.AsyncClient, order: dict) -> None:
        dep = order["deployment_id"]
        self.log(f"updating {self.version} -> {order['version']}")
        digest = hashlib.sha256()
        received = last_reported = 0
        speed = random.uniform(0.6, 1.4) * self.args.duration

        # Simulated network drop somewhere in the download (--net-fail-rate).
        drop_at = random.uniform(0.1, 0.9) if random.random() < self.args.net_fail_rate else None
        async with client.stream("GET", order["url"], headers=self.headers) as res:
            res.raise_for_status()
            if not await self.report(client, dep, "downloading", 0):
                return self.log("cancelled")
            async for chunk in res.aiter_bytes(4096):
                digest.update(chunk)
                received += len(chunk)
                pct = received * 100 // order["size"]
                # Pace the transfer so a whole download takes ~--duration seconds.
                await asyncio.sleep(speed * len(chunk) / order["size"])
                if drop_at is not None and received >= drop_at * order["size"]:
                    await self.report(client, dep, "failed", pct, error="connection lost")
                    return self.log(f"simulated network drop at {pct}%")
                if pct - last_reported >= 5:
                    last_reported = pct
                    if not await self.report(client, dep, "downloading", pct):
                        return self.log("cancelled by server, aborting")

        if digest.hexdigest() != order["sha256"]:
            await self.report(client, dep, "failed", error="sha256 mismatch")
            return self.log("sha256 mismatch")
        if random.random() < self.args.fail_rate / 2:
            await self.report(client, dep, "failed", error="flash write error")
            return self.log("simulated flash error")

        await self.report(client, dep, "rebooting", 100)
        await self.restart("software")
        if random.random() < self.args.fail_rate / 2:
            self.log("new image crashed, bootloader rolled back")
        else:
            self.version = order["version"]
            self.log(f"now running {self.version}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--interval", type=float, help="override the server's check-in interval")
    parser.add_argument("--duration", type=float, default=15, help="approx. seconds per download")
    parser.add_argument("--fail-rate", type=float, default=0.0, help="0..1, share of updates that fail")
    parser.add_argument("--net-fail-rate", type=float, default=0.0,
                        help="0..1, share of downloads cut by a (retryable) network error")
    parser.add_argument("--key", help="fleet key (X-Otter-Key)")
    parser.add_argument("--no-long-poll", action="store_true", help="plain periodic check-ins")
    parser.add_argument("--tokens", default=str(Path.home() / ".cache" / "otter-simulator.json"),
                        help="where fake devices keep their tokens between runs")
    parser.add_argument("--ca", help="CA certificate (PEM) to trust for an https:// server, e.g. Caddy's local CA")
    args = parser.parse_args()

    verify = ssl.create_default_context(cafile=args.ca) if args.ca else True
    async with httpx.AsyncClient(base_url=args.server, timeout=30, verify=verify) as client:
        if Path(args.tokens).exists():
            TOKENS.update(json.loads(Path(args.tokens).read_text()))
        devices = [FakeDevice(n, args) for n in range(args.count)]
        await asyncio.gather(*(d.run(client) for d in devices))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
