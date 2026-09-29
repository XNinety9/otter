"""Remote commands (#22): queued in the UI, delivered in check-ins, acknowledged by devices."""

import threading
import time
from datetime import timedelta

from otter.db import SessionLocal
from otter.models import Command
from test_channels import ids

A, B = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"


def send(client, macs, name, args=None):
    res = client.post("/api/commands", json={"device_ids": [ids(client)[m] for m in macs], "name": name, "args": args})
    assert res.status_code == 201, res.text
    return res.json()


def history(client, mac):
    return client.get(f"/api/devices/{ids(client)[mac]}/commands").json()


def test_command_round_trip(client, checkin):
    checkin(mac=A), checkin(mac=B)
    sent = send(client, [A, B], "blink", {"times": 3})
    assert [c["status"] for c in sent] == ["queued", "queued"]

    orders = checkin(mac=A)["commands"]
    assert orders == [{"id": sent[0]["id"], "name": "blink", "args": {"times": 3}}]
    assert checkin(mac=A)["commands"] == []  # delivered once
    assert history(client, A)[0]["status"] == "sent"

    res = client.post(f"/api/v1/commands/{orders[0]['id']}/result", json={"ok": True, "message": "blinked"})
    assert res.status_code == 200
    done = history(client, A)[0]
    assert (done["status"], done["result"]) == ("done", "blinked")
    assert history(client, B)[0]["status"] == "queued"  # B hasn't checked in yet


def test_failed_and_unknown(client, checkin):
    checkin(mac=A)
    send(client, [A], "fly")
    order = checkin(mac=A)["commands"][0]
    client.post(f"/api/v1/commands/{order['id']}/result", json={"ok": False, "message": "unknown command"})
    assert history(client, A)[0]["status"] == "failed"
    assert client.post("/api/v1/commands/999/result", json={"ok": True}).status_code == 404


def test_validation(client, checkin):
    checkin(mac=A)
    device = ids(client)[A]
    bad = [
        {"device_ids": [device], "name": "Reboot!"},
        {"device_ids": [], "name": "reboot"},
        {"device_ids": [device], "name": "x", "args": {"blob": "a" * 600}},
    ]
    for body in bad:
        assert client.post("/api/commands", json=body).status_code == 422
    assert client.post("/api/commands", json={"device_ids": [999], "name": "reboot"}).status_code == 404


def test_stale_commands_expire(client, checkin):
    checkin(mac=A)
    send(client, [A], "reboot")
    with SessionLocal() as session:
        command = session.query(Command).one()
        command.created_at -= timedelta(minutes=11)
        session.commit()
    assert checkin(mac=A)["commands"] == []
    assert history(client, A)[0]["status"] == "expired"


def test_a_long_polling_device_gets_it_at_once(client, checkin):
    checkin(mac=A)
    result = {}

    def poll():
        started = time.monotonic()
        res = client.post(
            "/api/v1/checkin", json={"mac": A, "hw": "esp32", "app": "weather", "fw_version": "1.0.0", "wait_s": 20}
        )
        result["commands"], result["took"] = res.json()["commands"], time.monotonic() - started

    thread = threading.Thread(target=poll)
    thread.start()
    time.sleep(0.5)
    send(client, [A], "identify")
    thread.join(10)
    assert [c["name"] for c in result["commands"]] == ["identify"]
    assert result["took"] < 5


def test_deleting_a_device_deletes_its_commands(client, checkin):
    checkin(mac=A)
    send(client, [A], "reboot")
    client.delete(f"/api/devices/{ids(client)[A]}")
    with SessionLocal() as session:
        assert session.query(Command).count() == 0
