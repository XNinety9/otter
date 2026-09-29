import json

import pytest

from otter import config
from otter.events import broadcaster
from otter.mqtt import HomeAssistantBridge
from test_ota_flow import device_id

MAC = "aa:bb:cc:00:00:01"
NODE = "otter_aabbcc000001"


class FakeClient:
    def __init__(self):
        self.messages = {}  # topic -> last payload (like retained messages)
        self.count = 0

    def publish(self, topic, payload, qos=0, retain=False):
        self.messages[topic] = payload
        self.count += 1


@pytest.fixture
def ha(client, monkeypatch):
    monkeypatch.setattr(config, "MQTT_INSTALL", False)
    bridge = HomeAssistantBridge()
    bridge.client = FakeClient()
    bridge.connected = True
    broadcaster.add_listener(bridge.on_event)
    yield bridge
    broadcaster.remove_listener(bridge.on_event)


def msg(bridge, topic):
    return json.loads(bridge.client.messages[topic])


def test_discovery_creates_entities(ha, checkin):
    checkin(mac=MAC)
    topics = set(ha.client.messages)
    assert {
        f"homeassistant/update/{NODE}/firmware/config",
        f"homeassistant/binary_sensor/{NODE}/online/config",
        f"homeassistant/sensor/{NODE}/rssi/config",
        f"homeassistant/sensor/{NODE}/ip/config",
        f"homeassistant/sensor/{NODE}/uptime/config",
    } <= topics
    update = msg(ha, f"homeassistant/update/{NODE}/firmware/config")
    assert update["device_class"] == "firmware" and update["unique_id"] == f"{NODE}_firmware"
    assert update["device"]["identifiers"] == [NODE] and update["device"]["model"] == "weather (esp32)"
    assert "command_topic" not in update  # install is opt-in
    state = msg(ha, "otter/aabbcc000001/state")
    assert state == {"online": "ON", "rssi": -60, "ip": "testclient", "uptime": None}


def test_update_available_and_progress(ha, client, checkin, upload):
    checkin(mac=MAC)
    firmware = msg(ha, "otter/aabbcc000001/firmware")
    assert (firmware["installed_version"], firmware["latest_version"]) == ("1.0.0", "1.0.0")

    fw = upload("1.1.0")
    assert msg(ha, "otter/aabbcc000001/firmware")["latest_version"] == "1.1.0"  # update available

    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    order = checkin(mac=MAC)["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "downloading", "progress": 40})
    firmware = msg(ha, "otter/aabbcc000001/firmware")
    assert firmware["in_progress"] is True and firmware["update_percentage"] == 40

    checkin(mac=MAC, version="1.1.0")
    firmware = msg(ha, "otter/aabbcc000001/firmware")
    assert (firmware["installed_version"], firmware["latest_version"], firmware["in_progress"]) == ("1.1.0", "1.1.0", False)
    assert msg(ha, f"homeassistant/update/{NODE}/firmware/config")["device"]["sw_version"] == "1.1.0"


def test_latest_version_follows_the_device_channel(ha, client, checkin):
    checkin(mac=MAC)
    ha.publish_all()
    ha._firmwares = [("weather", "esp32", "1.2.0", "beta"), ("weather", "esp32", "1.1.0", "stable")]
    device = client.get("/api/devices").json()[0]
    assert ha.latest_version(device) == "1.2.0"  # manual updates: newest in the registry
    assert ha.latest_version({**device, "channel": "stable"}) == "1.1.0"
    assert ha.latest_version({**device, "channel": "beta"}) == "1.2.0"
    assert ha.latest_version({**device, "fw_version": "2.0.0"}) == "2.0.0"  # nothing newer


def test_only_changes_are_republished(ha, checkin):
    checkin(mac=MAC)
    before = ha.client.count
    ha.publish_all()
    assert ha.client.count == before


def test_forgotten_device_is_removed(ha, client, checkin):
    checkin(mac=MAC)
    client.delete(f"/api/devices/{device_id(client)}")
    assert ha.client.messages[f"homeassistant/update/{NODE}/firmware/config"] == ""


def test_install_from_home_assistant(ha, client, checkin, upload, monkeypatch):
    checkin(mac=MAC)
    upload("1.1.0")
    ha.publish_all()
    assert ha.install("aabbcc000001") is None  # disabled by default

    monkeypatch.setattr(config, "MQTT_INSTALL", True)
    ha.publish_all()
    assert msg(ha, f"homeassistant/update/{NODE}/firmware/config")["command_topic"] == "otter/aabbcc000001/install"
    assert ha.install("aabbcc000001") is not None
    assert checkin(mac=MAC)["update"]["version"] == "1.1.0"
    assert ha.install("aabbcc000001") is None  # already being deployed
