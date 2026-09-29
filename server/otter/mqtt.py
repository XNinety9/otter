"""Home Assistant integration through MQTT discovery.

With OTTER_MQTT_URL set, Otter publishes on the broker, for each device, the entities Home
Assistant creates by itself:

    update          Firmware: installed and latest version, progress while updating
    binary_sensor   Connectivity (diagnostic)
    sensor          Wi-Fi signal, IP address, uptime (diagnostic)

The "latest version" is the newest firmware for the device's app and hardware: from its
channels if it follows one, from the whole registry otherwise. With OTTER_MQTT_INSTALL,
the update entity's Install button deploys it.

Everything is retained on the broker and republished only when it changes. The bridge
follows the same events as the web UI, plus a periodic pass for online/offline changes.
"""

import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import paho.mqtt.client as mqtt
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import config
from .channels import STABLE
from .db import SessionLocal, utcnow
from .events import broadcaster
from .models import OPEN_STATES, Deployment, Device, Firmware
from .schemas import DeviceOut
from .versions import version_key

log = logging.getLogger("otter.mqtt")

ACTIVE = ("pending", "downloading", "rebooting")


def node_id(mac: str) -> str:
    return "otter_" + mac.replace(":", "")


class HomeAssistantBridge:
    def __init__(self) -> None:
        self.client: mqtt.Client | None = None
        self.connected = False
        self._published: dict[str, str] = {}  # topic -> last payload, to publish changes only
        self._devices: dict[int, str] = {}  # device id -> mac, to clean up deleted devices
        self._firmwares: list[tuple[str, str, str, str | None]] = []  # (app, hw, version, channel)
        self._lock = threading.Lock()

    # --- Topics ----------------------------------------------------------------

    @property
    def base(self) -> str:
        return config.MQTT_BASE_TOPIC

    def status_topic(self) -> str:
        return f"{self.base}/status"

    def firmware_topic(self, mac: str) -> str:
        return f"{self.base}/{node_id(mac).removeprefix('otter_')}/firmware"

    def state_topic(self, mac: str) -> str:
        return f"{self.base}/{node_id(mac).removeprefix('otter_')}/state"

    def install_topic(self, mac: str) -> str:
        return f"{self.base}/{node_id(mac).removeprefix('otter_')}/install"

    def config_topic(self, component: str, mac: str, key: str) -> str:
        return f"{config.MQTT_DISCOVERY_PREFIX}/{component}/{node_id(mac)}/{key}/config"

    # --- Connection ------------------------------------------------------------

    def start(self) -> None:
        if not config.MQTT_URL:
            return
        url = urlsplit(config.MQTT_URL)
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"otter-{uuid.uuid4().hex[:8]}"
        )
        if url.username:
            client.username_pw_set(unquote(url.username), unquote(url.password or ""))
        if url.scheme == "mqtts":
            client.tls_set()
        client.will_set(self.status_topic(), "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client = client
        port = url.port or (8883 if url.scheme == "mqtts" else 1883)
        client.connect_async(url.hostname, port, keepalive=60)
        client.loop_start()
        broadcaster.add_listener(self.on_event)
        log.info("MQTT bridge connecting to %s:%s", url.hostname, port)

    def stop(self) -> None:
        if self.client is None:
            return
        broadcaster.remove_listener(self.on_event)
        if self.connected:
            self.client.publish(self.status_topic(), "offline", qos=1, retain=True).wait_for_publish(2)
        self.client.disconnect()
        self.client.loop_stop()
        self.client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connection refused: %s", reason_code)
            return
        self.connected = True
        with self._lock:
            self._published.clear()  # the broker may have lost retained messages: send everything
        client.publish(self.status_topic(), "online", qos=1, retain=True)
        if config.MQTT_INSTALL:
            client.subscribe(f"{self.base}/+/install", qos=1)
        self.publish_all()
        log.info("MQTT bridge connected")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False

    def _publish(self, topic: str, payload: dict | str, retain: bool = True) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
        with self._lock:
            if self._published.get(topic) == text:
                return
            self._published[topic] = text
        if self.client is not None and self.connected:
            self.client.publish(topic, text, qos=1, retain=retain)

    # --- Publishing ------------------------------------------------------------

    def on_event(self, kind: str, data: object) -> None:
        if not self.connected:
            return
        if kind == "device":
            self.publish_device(data)
        elif kind == "device_deleted":
            self.remove_device(data["id"])
        elif kind in ("firmwares", "resync"):
            self.publish_all()

    def publish_all(self) -> None:
        """Every device, e.g. after connecting, when firmwares change, and periodically for online/offline."""
        with SessionLocal() as session:
            self._firmwares = [
                tuple(row) for row in session.execute(select(Firmware.app, Firmware.hw, Firmware.version, Firmware.channel))
            ]
            devices = session.scalars(select(Device).options(selectinload(Device.tags), selectinload(Device.deployments))).all()
            payloads = [DeviceOut.model_validate(d).model_dump(mode="json") for d in devices]
        for device in payloads:
            self.publish_device(device)
        for device_id in set(self._devices) - {d["id"] for d in payloads}:
            self.remove_device(device_id)

    def latest_version(self, device: dict) -> str:
        """Newest firmware the device could run; its own version when nothing is newer."""
        channels = {device["channel"], STABLE} if device.get("channel") else None
        candidates = [
            version
            for app, hw, version, channel in self._firmwares
            if app == device["app"] and hw == device["hw"] and (channels is None or channel in channels)
        ]
        newest = max(candidates, key=version_key, default=device["fw_version"])
        return max(newest, device["fw_version"], key=version_key)

    def publish_device(self, device: dict) -> None:
        mac = device["mac"]
        self._devices[device["id"]] = mac
        label = device["name"] or mac
        info = {
            "identifiers": [node_id(mac)],
            "connections": [["mac", mac]],
            "name": label,
            "manufacturer": "Otter",
            "model": f"{device['app']} ({device['hw']})",
            "sw_version": device["fw_version"],
        }
        if config.PUBLIC_URL:
            info["configuration_url"] = config.PUBLIC_URL
        common = {"device": info, "availability": [{"topic": self.status_topic()}], "origin": {"name": "Otter"}}
        uid = node_id(mac)

        update = {
            **common,
            "name": "Firmware",
            "unique_id": f"{uid}_firmware",
            "device_class": "firmware",
            "state_topic": self.firmware_topic(mac),
        }
        if config.MQTT_INSTALL:
            update |= {"command_topic": self.install_topic(mac), "payload_install": "install"}
        self._publish(self.config_topic("update", mac, "firmware"), update)

        state = self.state_topic(mac)
        diagnostic = {**common, "state_topic": state, "entity_category": "diagnostic"}
        self._publish(
            self.config_topic("binary_sensor", mac, "online"),
            {**diagnostic, "name": "Connectivity", "unique_id": f"{uid}_online", "device_class": "connectivity",
             "value_template": "{{ value_json.online }}"},
        )
        self._publish(
            self.config_topic("sensor", mac, "rssi"),
            {**diagnostic, "name": "Wi-Fi signal", "unique_id": f"{uid}_rssi", "device_class": "signal_strength",
             "unit_of_measurement": "dBm", "state_class": "measurement", "value_template": "{{ value_json.rssi }}"},
        )
        self._publish(
            self.config_topic("sensor", mac, "ip"),
            {**diagnostic, "name": "IP address", "unique_id": f"{uid}_ip", "icon": "mdi:ip-network",
             "value_template": "{{ value_json.ip }}"},
        )
        self._publish(
            self.config_topic("sensor", mac, "uptime"),
            {**diagnostic, "name": "Uptime", "unique_id": f"{uid}_uptime", "device_class": "duration",
             "unit_of_measurement": "s", "value_template": "{{ value_json.uptime }}"},
        )

        deployment = device.get("last_deployment") or {}
        updating = deployment.get("status") in ACTIVE
        firmware = {
            "installed_version": device["fw_version"],
            "latest_version": deployment["firmware"]["version"] if updating else self.latest_version(device),
            "title": f"{device['app']} firmware",
            "in_progress": updating,
            "update_percentage": deployment.get("progress") if updating else None,
        }
        if config.PUBLIC_URL:
            firmware["release_url"] = config.PUBLIC_URL
        self._publish(self.firmware_topic(mac), firmware)

        last_seen = datetime.fromisoformat(device["last_seen"])
        online = datetime.now(UTC) - last_seen < timedelta(seconds=config.ONLINE_TIMEOUT_S)
        self._publish(
            state,
            {"online": "ON" if online else "OFF", "rssi": device["rssi"], "ip": device["ip"], "uptime": device["uptime_s"]},
        )

    def remove_device(self, device_id: int) -> None:
        mac = self._devices.pop(device_id, None)
        if mac is None:
            return
        # An empty retained config removes the entity from Home Assistant.
        for component, key in [("update", "firmware"), ("binary_sensor", "online"), ("sensor", "rssi"),
                               ("sensor", "ip"), ("sensor", "uptime")]:
            self._publish(self.config_topic(component, mac, key), "")
        for topic in (self.firmware_topic(mac), self.state_topic(mac)):
            self._publish(topic, "")

    # --- Install from Home Assistant -------------------------------------------

    def _on_message(self, client, userdata, message) -> None:
        try:
            if message.topic.endswith("/install") and message.payload.decode() == "install":
                self.install(message.topic.split("/")[-2])
        except Exception:
            log.exception("MQTT command on %s failed", message.topic)

    def install(self, mac_id: str) -> Deployment | None:
        """Deploys the latest version to a device (Home Assistant's Install button)."""
        from .api_device import publish_device  # avoid an import cycle
        from .events import wakeups

        if not config.MQTT_INSTALL:
            return None
        with SessionLocal() as session:
            device = next(
                (d for d in session.scalars(select(Device)).all() if d.mac.replace(":", "") == mac_id.lower()), None
            )
            if device is None or any(d.status in OPEN_STATES for d in device.deployments):
                return None
            payload = DeviceOut.model_validate(device).model_dump(mode="json")
            latest = self.latest_version(payload)
            if latest == device.fw_version:
                return None
            firmware = session.scalar(
                select(Firmware).where(Firmware.app == device.app, Firmware.hw == device.hw, Firmware.version == latest)
            )
            deployment = Deployment(firmware=firmware, created_at=utcnow())
            device.deployments.append(deployment)
            session.commit()
            publish_device(device)
            wakeups.notify(device.mac)
            log.info("install of %s %s on %s requested from Home Assistant", firmware.app, latest, device.mac)
            return deployment


bridge = HomeAssistantBridge()
