"""Remote configuration (#23): per tag and per device, delivered when the version changes."""

import threading
import time

from test_channels import ids

A, B = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"


def checkin(client, mac, version=None, **extra):
    body = {"mac": mac, "hw": "esp32", "app": "weather", "fw_version": "1.0.0", **extra}
    if version is not None:
        body["config_version"] = version
    res = client.post("/api/v1/checkin", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def put(client, path, values):
    res = client.put(path, json={"values": values})
    assert res.status_code == 200, res.text
    return res.json()


def test_device_gets_its_config_once(client):
    checkin(client, A, "")
    assert checkin(client, A, "")["config"] is None  # nothing configured
    put(client, f"/api/devices/{ids(client)[A]}/config", {"interval": 60, "name": "desk", "debug": True})

    config = checkin(client, A, "")["config"]
    assert config["values"] == {"debug": True, "interval": 60, "name": "desk"}
    assert checkin(client, A, config["version"])["config"] is None  # up to date
    state = client.get(f"/api/devices/{ids(client)[A]}/config").json()
    assert state["reported_version"] == state["version"] == config["version"]


def test_tags_and_device_overrides(client):
    checkin(client, A, ""), checkin(client, B, "")
    client.patch(f"/api/devices/{ids(client)[A]}", json={"tags": ["kitchen", "sensors"]})
    client.patch(f"/api/devices/{ids(client)[B]}", json={"tags": ["kitchen"]})
    put(client, "/api/tags/kitchen/config", {"interval": 30, "unit": "C"})
    put(client, "/api/tags/sensors/config", {"interval": 10})  # alphabetical: sensors wins over kitchen
    put(client, f"/api/devices/{ids(client)[A]}/config", {"unit": "F"})

    state = client.get(f"/api/devices/{ids(client)[A]}/config").json()
    assert state["values"] == {"interval": 10, "unit": "F"}
    assert state["sources"] == {"interval": "#sensors", "unit": "device"}
    assert state["own"] == {"unit": "F"}
    assert checkin(client, B, "")["config"]["values"] == {"interval": 30, "unit": "C"}

    # Removing a key from the device falls back to the tags'.
    put(client, f"/api/devices/{ids(client)[A]}/config", {})
    assert checkin(client, A, "")["config"]["values"] == {"interval": 10, "unit": "C"}


def test_agents_without_config_support_never_get_one(client):
    checkin(client, A)
    put(client, f"/api/devices/{ids(client)[A]}/config", {"interval": 60})
    assert checkin(client, A)["config"] is None


def test_validation(client):
    checkin(client, A, "")
    path = f"/api/devices/{ids(client)[A]}/config"
    for values in [{"Bad-Key": 1}, {"nested": {"a": 1}}, {"list": [1, 2]}, {"long": "x" * 300}]:
        assert client.put(path, json={"values": values}).status_code == 422, values
    assert client.put("/api/tags/Not A Tag!/config", json={"values": {}}).status_code == 422


def test_a_long_polling_device_gets_a_change_at_once(client):
    checkin(client, A, "")
    result = {}

    def poll():
        started = time.monotonic()
        result["config"] = checkin(client, A, "", wait_s=20)["config"]
        result["took"] = time.monotonic() - started

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.5)
    put(client, f"/api/devices/{ids(client)[A]}/config", {"interval": 5})
    thread.join(10)
    assert result["config"]["values"] == {"interval": 5}
    assert result["took"] < 5


def test_deleting_a_device_deletes_its_config(client):
    from otter.db import SessionLocal
    from otter.models import ConfigValue

    checkin(client, A, "")
    put(client, f"/api/devices/{ids(client)[A]}/config", {"interval": 5})
    client.delete(f"/api/devices/{ids(client)[A]}")
    with SessionLocal() as session:
        assert session.query(ConfigValue).count() == 0


def test_a_wake_up_without_news_keeps_the_device_waiting(client):
    checkin(client, A, "")
    result = {}

    def poll():
        started = time.monotonic()
        result["config"] = checkin(client, A, "", wait_s=20)["config"]
        result["took"] = time.monotonic() - started

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.5)
    client.patch(f"/api/devices/{ids(client)[A]}", json={"tags": ["kitchen"]})  # wakes it, nothing new
    time.sleep(1)
    put(client, "/api/tags/kitchen/config", {"interval": 5})
    thread.join(10)
    assert result["config"]["values"] == {"interval": 5}  # still waiting when the value came
    assert 1 < result["took"] < 5
