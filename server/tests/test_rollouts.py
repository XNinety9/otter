from datetime import timedelta

import pytest

from otter import rollouts
from otter.db import SessionLocal, utcnow


def macs(n):
    return [f"aa:bb:cc:00:00:{i:02x}" for i in range(1, n + 1)]


@pytest.fixture
def fleet(client, checkin, upload):
    """10 weather/esp32 devices on 1.0.0 and a 1.1.0 firmware."""
    for mac in macs(10):
        checkin(mac=mac)
    return upload("1.1.0")


def evaluate(now=None):
    with SessionLocal() as session:
        changes = rollouts.evaluate(session, now or utcnow())
        session.commit()
        return changes


def statuses(client):
    """{mac: status of its last deployment}"""
    return {d["mac"]: (d["last_deployment"] or {}).get("status") for d in client.get("/api/devices").json()}


def start(client, fw, **body):
    res = client.post("/api/rollouts", json={"firmware_id": fw["id"], "soak_s": 60, **body})
    assert res.status_code == 201, res.text
    return res.json()


def finish(checkin, client, status, version="1.1.0", error="boom"):
    """Every device whose deployment is pending succeeds (or fails)."""
    for mac, st in statuses(client).items():
        if st != "pending":
            continue
        order = checkin(mac=mac)["update"]
        if status == "success":
            checkin(mac=mac, version=version)
        else:
            client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": error})


def test_stages_split_targets_and_queue_later_ones(client, checkin, fleet):
    r = start(client, fleet)
    assert [s["size"] for s in r["stages"]] == [1, 4, 5]
    counts = list(statuses(client).values())
    assert counts.count("pending") == 1 and counts.count("queued") == 9
    queued = next(mac for mac, st in statuses(client).items() if st == "queued")
    assert checkin(mac=queued)["update"] is None  # devices never see queued deployments


def test_next_stage_waits_for_the_soak_time(client, checkin, fleet):
    start(client, fleet)
    finish(checkin, client, "success")
    t0 = utcnow()
    evaluate(t0)  # stage 1 done: soak starts
    assert list(statuses(client).values()).count("pending") == 0
    evaluate(t0 + timedelta(seconds=30))
    assert list(statuses(client).values()).count("pending") == 0
    changes = evaluate(t0 + timedelta(seconds=61))
    assert list(statuses(client).values()).count("pending") == 4
    assert len(changes.wake) == 4


def test_whole_rollout_completes(client, checkin, fleet):
    r = start(client, fleet, soak_s=0)
    for _ in range(3):
        finish(checkin, client, "success")
        evaluate()
    rollout = client.get("/api/rollouts").json()[0]
    assert rollout["status"] == "completed"
    assert [s["success"] for s in rollout["stages"]] == [1, 4, 5]
    assert set(statuses(client).values()) == {"success"}
    assert rollout["id"] == r["id"]


def test_failing_canary_halts_before_the_rest_of_the_fleet(client, checkin, fleet):
    start(client, fleet, soak_s=0)
    finish(checkin, client, "failed")
    changes = evaluate()
    rollout = client.get("/api/rollouts").json()[0]
    assert rollout["status"] == "halted"
    assert rollout["message"] == "stage 1: 1/1 failed"
    assert [h.id for h in changes.halted] == [rollout["id"]]
    assert list(statuses(client).values()).count("queued") == 9

    # A human looked at it and decides to go on anyway.
    res = client.post(f"/api/rollouts/{rollout['id']}/advance")
    assert res.json()["status"] == "running" and res.json()["current_stage"] == 1
    assert list(statuses(client).values()).count("pending") == 4


def test_failures_within_the_threshold_do_not_halt(client, checkin, fleet):
    start(client, fleet, soak_s=0, stages=[50, 100], max_failure_rate=0.2)  # stage 1 = 5 devices, 1 failure allowed
    pending = [mac for mac, st in statuses(client).items() if st == "pending"]
    order = checkin(mac=pending[0])["update"]
    client.post(f"/api/v1/deployments/{order['deployment_id']}/progress", json={"state": "failed", "error": "x"})
    finish(checkin, client, "success")
    evaluate()
    assert client.get("/api/rollouts").json()[0]["current_stage"] == 1


def test_pause_resume_and_abort(client, checkin, fleet):
    r = start(client, fleet, soak_s=0)
    assert client.post(f"/api/rollouts/{r['id']}/pause").json()["status"] == "paused"
    finish(checkin, client, "success")
    evaluate()
    assert client.get("/api/rollouts").json()[0]["current_stage"] == 0  # paused: no progression
    assert client.post(f"/api/rollouts/{r['id']}/resume").json()["status"] == "running"
    evaluate()
    assert client.get("/api/rollouts").json()[0]["current_stage"] == 1

    assert client.post(f"/api/rollouts/{r['id']}/abort").json()["status"] == "aborted"
    assert list(statuses(client).values()).count("cancelled") == 9  # 4 pending + 5 queued
    assert client.post(f"/api/rollouts/{r['id']}/resume").status_code == 409


def test_manual_deployment_takes_a_device_out_of_the_rollout(client, checkin, fleet, upload):
    start(client, fleet, soak_s=0)
    queued_mac = next(mac for mac, st in statuses(client).items() if st == "queued")
    device = next(d for d in client.get("/api/devices").json() if d["mac"] == queued_mac)
    other = upload("1.2.0")
    client.post("/api/deployments", json={"firmware_id": other["id"], "device_ids": [device["id"]]})

    history = client.get(f"/api/devices/{device['id']}/deployments").json()
    assert [(h["firmware"]["version"], h["status"]) for h in history] == [("1.2.0", "pending"), ("1.1.0", "cancelled")]


def test_targets_skip_up_to_date_devices_and_respect_tags(client, checkin, upload):
    for mac in macs(4):
        checkin(mac=mac)
    checkin(mac="aa:bb:cc:00:00:05", version="1.1.0")  # already there
    fw = upload("1.1.0")
    ids = {d["mac"]: d["id"] for d in client.get("/api/devices").json()}
    for mac in macs(2) + ["aa:bb:cc:00:00:05"]:
        client.patch(f"/api/devices/{ids[mac]}", json={"tags": ["lab"]})

    r = start(client, fw, tags=["lab"])
    assert sum(s["size"] for s in r["stages"]) == 2
    assert r["tags"] == ["lab"]

    res = client.post("/api/rollouts", json={"firmware_id": fw["id"], "tags": ["nowhere"]})
    assert res.status_code == 422


@pytest.mark.parametrize("stages", [[50], [0, 100], [50, 20, 100], [10, 10, 100], [10, 150]])
def test_invalid_stages(client, fleet, stages):
    res = client.post("/api/rollouts", json={"firmware_id": fleet["id"], "stages": stages})
    assert res.status_code == 422
