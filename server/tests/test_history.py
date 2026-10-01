"""Signal and memory history (#98)."""

from datetime import timedelta

import pytest

from otter import history
from otter.db import SessionLocal, utcnow
from otter.models import DeviceSample
from test_channels import ids

MAC = "aa:bb:cc:00:00:01"


@pytest.fixture(autouse=True)
def fresh_history():
    history._last_sample.clear()
    history._pruned_at = None
    yield


def get(client, hours=24):
    return client.get(f"/api/devices/{ids(client)[MAC]}/history?hours={hours}").json()


def test_one_sample_every_five_minutes(client, checkin):
    for heap in (200_000, 199_000, 198_000):
        checkin(free_heap=heap, uptime_s=100)
    samples = get(client)["samples"]
    assert [(s["rssi"], s["free_heap"]) for s in samples] == [(-60, 200_000)]


def test_a_restart_is_marked_at_once(client, checkin):
    checkin(free_heap=200_000, uptime_s=500, boot_count=1, reset_reason="power_on")
    checkin(free_heap=210_000, uptime_s=3, boot_count=2, reset_reason="panic")
    data = get(client)
    assert [s["free_heap"] for s in data["samples"]] == [200_000, 210_000]
    assert [r["reason"] for r in data["restarts"]] == ["panic"]


def test_old_samples_are_pruned_and_ranges_filter(client, checkin):
    checkin(free_heap=200_000)
    device_id = ids(client)[MAC]
    with SessionLocal() as session:
        for days in (1.5, 8):
            session.add(DeviceSample(device_id=device_id, at=utcnow() - timedelta(days=days), rssi=-70, free_heap=1))
        session.commit()
    assert len(get(client, 24)["samples"]) == 1
    assert len(get(client, 168)["samples"]) == 2

    def stored():
        with SessionLocal() as session:
            return session.query(DeviceSample).count()

    assert stored() == 3  # pruning runs at most hourly
    history._pruned_at = None
    history._last_sample.clear()
    checkin(free_heap=200_000)
    assert stored() == 3  # the 8-day-old one is gone, a new one came


def test_samples_go_with_the_device(client, checkin):
    checkin(free_heap=200_000)
    device_id = ids(client)[MAC]
    client.delete(f"/api/devices/{device_id}")
    with SessionLocal() as session:
        assert session.query(DeviceSample).count() == 0
