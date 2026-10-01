"""Live logs from devices (#96)."""

import pytest

from otter.logs import device_logs
from test_channels import ids

MAC = "aa:bb:cc:00:00:01"


@pytest.fixture(autouse=True)
def clean_logs():
    yield
    device_logs.__init__()


def send(client, lines, mac=MAC):
    return client.post("/api/v1/logs", json={"mac": mac, "lines": lines})


def test_watching_asks_the_device_once(client, checkin):
    assert checkin(logs=True).get("logs_s") is None
    device_id = ids(client)[MAC]
    assert client.post(f"/api/devices/{device_id}/logs/watch").json() == {"watch_s": 600}
    assert 590 <= checkin(logs=True)["logs_s"] <= 600
    assert checkin(logs=True)["logs_s"] is None  # already told: long polls keep waiting
    client.post(f"/api/devices/{device_id}/logs/stop")
    assert checkin(logs=True)["logs_s"] == 0
    assert checkin(logs=True)["logs_s"] is None


def test_agents_without_logs_are_not_asked(client, checkin):
    checkin()
    client.post(f"/api/devices/{ids(client)[MAC]}/logs/watch")
    assert checkin()["logs_s"] is None  # its long poll isn't cut short for nothing


def test_lines_are_kept_and_capped(client, checkin):
    checkin()
    device_id = ids(client)[MAC]
    assert send(client, ["I (12) otter: hello  ", "W (20) wifi: weak"]).status_code == 204
    assert [line["text"] for line in client.get(f"/api/devices/{device_id}/logs").json()] == [
        "I (12) otter: hello", "W (20) wifi: weak",
    ]
    for i in range(6):
        send(client, [f"line {i}-{n}" for n in range(100)])
    lines = client.get(f"/api/devices/{device_id}/logs").json()
    assert len(lines) == 500 and lines[-1]["text"] == "line 5-99"


def test_unknown_device_and_limits(client, checkin):
    assert send(client, ["hi"]).status_code == 404
    checkin()
    assert send(client, ["x" * 600]).status_code == 422
    assert send(client, ["x"] * 201).status_code == 422


def test_a_watch_wakes_a_long_poll(client, checkin):
    import threading
    import time

    checkin()
    device_id = ids(client)[MAC]
    result = {}

    def poll():
        start = time.monotonic()
        result["answer"] = checkin(wait_s=20, logs=True)
        result["took"] = time.monotonic() - start

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.5)
    client.post(f"/api/devices/{device_id}/logs/watch")
    thread.join(10)
    assert result["answer"]["logs_s"] > 0 and result["took"] < 5
