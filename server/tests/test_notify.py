from datetime import timedelta

import pytest

from otter import config, rollouts
from otter.db import SessionLocal, utcnow
from otter.models import Device
from otter.notify import Message, notifier, parse_targets, render
from test_ota_flow import device_id


@pytest.fixture
def sent(client, monkeypatch):
    """Notifications go to an ntfy topic and a generic webhook; returns what was posted."""
    notifier.stop()  # tests process the queue synchronously with drain()
    monkeypatch.setattr(config, "NOTIFY_URLS", "ntfy+https://ntfy.example/otter, https://hooks.example/otter")
    posted = []
    monkeypatch.setattr(notifier, "post", lambda url, headers, payload: posted.append((url, payload)))

    def flush():
        notifier.drain()
        return posted

    return flush


def titles(posted, host="ntfy.example"):
    return [payload["title"] for url, payload in posted if host in url]


def test_targets_and_rendering(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://otter.lan:8000")
    targets = parse_targets(
        "ntfy+https://alice:s3cret@ntfy.example:8443/alerts https://discord.com/api/webhooks/1/tok,https://x.example/h"
    )
    assert [t.kind for t in targets] == ["ntfy", "discord", "webhook"]
    assert targets[0].describe() == "ntfy ntfy.example"  # no credentials, path or token

    msg = Message("deployment_failed", "Update failed on desk — garden", "weather 1.1.0: boom", "error", {"app": "weather"})
    url, headers, payload = render(targets[0], msg)
    assert url == "https://ntfy.example:8443/"
    assert headers["Authorization"] == "Basic YWxpY2U6czNjcmV0"
    assert payload == {
        "topic": "alerts",
        "title": "Update failed on desk — garden",
        "message": "weather 1.1.0: boom",
        "priority": 4,
        "tags": ["warning"],
        "click": "http://otter.lan:8000",
    }
    _, _, payload = render(targets[1], msg)
    assert payload["embeds"][0]["title"] == msg.title and payload["embeds"][0]["color"] == 0xD64545
    _, _, payload = render(targets[2], msg)
    assert payload["event"] == "deployment_failed" and payload["data"] == {"app": "weather"}


def test_failed_deployment_is_notified_to_every_target(sent, client, checkin, upload):
    checkin()
    sent()  # the new-device notification
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    order = checkin()["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": "sha256 mismatch"})

    posted = sent()
    assert titles(posted) == ["New device: aa:bb:cc:00:00:01", "Update failed on aa:bb:cc:00:00:01"]
    webhook = [p for url, p in posted if "hooks.example" in url][-1]
    assert webhook["message"] == "weather 1.1.0: sha256 mismatch"


def test_rollback_is_notified_as_a_failure(sent, client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/deployments", json={"firmware_id": fw["id"], "device_ids": [device_id(client)]})
    order = checkin()["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "rebooting"})
    checkin("1.0.0")
    assert "weather 1.1.0: came back running 1.0.0 (rollback?)" in [p["message"] for _, p in sent()]


def test_offline_then_back_online(sent, client, checkin):
    checkin()
    notifier.seed_offline()
    sent()
    later = utcnow() + timedelta(minutes=config.NOTIFY_OFFLINE_MINUTES + 1)
    notifier.check_offline(now=later)
    notifier.check_offline(now=later)  # no duplicate alert
    assert titles(sent())[-1:] == ["aa:bb:cc:00:00:01 is offline"]
    assert titles(sent()).count("aa:bb:cc:00:00:01 is offline") == 1

    checkin()
    assert titles(sent())[-1] == "aa:bb:cc:00:00:01 is back online"


def test_devices_already_offline_at_startup_are_not_alerted(sent, client, checkin):
    checkin()
    with SessionLocal() as session:
        session.get(Device, device_id(client)).last_seen = utcnow() - timedelta(days=3)
        session.commit()
    notifier.seed_offline()
    before = len(sent())
    notifier.check_offline()
    assert len(sent()) == before


def test_halted_rollout_is_notified(sent, client, checkin, upload):
    checkin()
    fw = upload("1.1.0")
    client.post("/api/rollouts", json={"firmware_id": fw["id"], "soak_s": 0})
    order = checkin()["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": "boom"})
    with SessionLocal() as session:
        changes = rollouts.evaluate(session)
        session.commit()
    for rollout in changes.halted:
        notifier.emit("rollout_halted", rollout_id=rollout.id)
    posted = sent()
    assert "Rollout halted: weather 1.1.0" in titles(posted)
    assert any("(rollout #1, stage 1)" in p["message"] for _, p in posted)


def test_a_broken_target_does_not_stop_the_others(sent, client, monkeypatch):
    delivered = []

    def flaky(url, headers, payload):
        if "ntfy" in url:
            raise OSError("connection refused")
        delivered.append(url)

    monkeypatch.setattr(notifier, "post", flaky)
    res = client.post("/api/notifications/test").json()["results"]
    assert res == [
        {"target": "ntfy ntfy.example", "ok": False, "error": "connection refused"},
        {"target": "webhook hooks.example", "ok": True, "error": None},
    ]
    assert delivered == ["https://hooks.example/otter"]


def test_settings_and_test_endpoint_without_targets(client, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_URLS", "")
    assert client.get("/api/notifications").json()["targets"] == []
    assert client.post("/api/notifications/test").status_code == 409
    notifier.emit("device_new", device_id=1)
    assert notifier.queue.empty()  # nothing is queued when notifications are off
