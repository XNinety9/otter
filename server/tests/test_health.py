"""Device health: heap, reset reason, boot count, and crash detection (#12)."""

from test_metrics import scrape, value
from test_notify import sent, titles  # noqa: F401 (fixture)

MAC = "aa:bb:cc:00:00:01"


def boot(checkin, count, reason, uptime=5, **extra):
    return checkin(reset_reason=reason, boot_count=count, uptime_s=uptime, **extra)


def test_health_fields_are_stored(client, checkin):
    boot(checkin, 3, "power_on", free_heap=284104, min_free_heap=251360)
    device = client.get("/api/devices").json()[0]
    assert (device["free_heap"], device["min_free_heap"], device["reset_reason"], device["boot_count"]) == (
        284104, 251360, "power_on", 3,
    )


def test_invalid_reset_reason_is_refused(client):
    res = client.post(
        "/api/v1/checkin",
        json={"mac": MAC, "hw": "esp32", "app": "weather", "fw_version": "1.0.0", "reset_reason": "<b>boom</b>"},
    )
    assert res.status_code == 422


def test_a_crash_is_notified_once(sent, client, checkin):  # noqa: F811
    before = scrape(client)
    boot(checkin, 1, "power_on")
    boot(checkin, 1, "power_on", uptime=65)
    boot(checkin, 2, "panic")  # rebooted after a panic
    boot(checkin, 2, "panic", uptime=65)  # same boot: no new alert
    boot(checkin, 3, "software")  # e.g. after an update: not a crash
    boot(checkin, 4, "brownout")
    after = scrape(client)

    assert titles(sent()) == [
        f"New device: {MAC}",
        f"{MAC} restarted after a panic",
        f"{MAC} restarted after a brownout",
    ]
    crashes = lambda m, reason: value(m, "otter_device_crashes_total", app="weather", reason=reason)  # noqa: E731
    assert crashes(after, "panic") - crashes(before, "panic") == 1
    assert crashes(after, "brownout") - crashes(before, "brownout") == 1


def test_without_boot_count_uptime_going_down_means_a_restart(sent, checkin):  # noqa: F811
    checkin(reset_reason="power_on", uptime_s=100)
    checkin(reset_reason="task_watchdog", uptime_s=130)  # same boot, reason unchanged in practice
    checkin(reset_reason="task_watchdog", uptime_s=4)
    assert titles(sent())[1:] == [f"{MAC} restarted after a task watchdog"]
