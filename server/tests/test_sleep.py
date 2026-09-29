"""Sleeping devices (#20): online means "seen within their own announced interval"."""

from datetime import timedelta

from otter import config
from otter.db import SessionLocal, utcnow
from otter.models import Device
from otter.notify import notifier
from test_metrics import scrape, value
from test_notify import sent, titles  # noqa: F401 (fixture)

SLEEPY, AWAKE = "aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"


def age(minutes: float) -> None:
    """Makes every device's last check-in that old."""
    with SessionLocal() as session:
        for device in session.query(Device):
            device.last_seen = utcnow() - timedelta(minutes=minutes)
        session.commit()


def test_a_sleeping_device_stays_online_until_its_next_checkin(client, checkin):
    checkin(mac=SLEEPY, next_checkin_s=3600)
    checkin(mac=AWAKE)
    assert {d["mac"]: d["next_checkin_s"] for d in client.get("/api/devices").json()} == {SLEEPY: 3600, AWAKE: None}

    age(30)
    metrics = scrape(client)
    assert value(metrics, "otter_devices", app="weather", hw="esp32", version="1.0.0", online="true") == 1
    assert value(metrics, "otter_devices", app="weather", hw="esp32", version="1.0.0", online="false") == 1

    age(90)  # an hour and a half: late even for a sleeper
    metrics = scrape(client)
    assert value(metrics, "otter_devices", app="weather", hw="esp32", version="1.0.0", online="false") == 2


def test_offline_alerts_wait_for_the_sleep_to_be_over(sent, client, checkin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "NOTIFY_OFFLINE_MINUTES", 10)
    checkin(mac=SLEEPY, next_checkin_s=3600)
    checkin(mac=AWAKE)
    notifier.seed_offline()
    age(30)
    notifier.check_offline()
    assert titles(sent())[-1] == f"{AWAKE} is offline"
    age(90)
    notifier.check_offline()
    assert titles(sent())[-1] == f"{SLEEPY} is offline"


def test_a_device_that_stops_sleeping_counts_normally_again(client, checkin):
    checkin(mac=SLEEPY, next_checkin_s=3600)
    checkin(mac=SLEEPY)  # no next_checkin_s anymore
    assert client.get("/api/devices").json()[0]["next_checkin_s"] is None
