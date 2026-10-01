"""The exact chip a device runs on (#74)."""

MAC = "aa:bb:cc:00:00:01"


def test_chip_is_stored(client, checkin):
    checkin(chip="ESP32-C6FH4 (QFN32)", chip_rev="0.1")
    device = client.get("/api/devices").json()[0]
    assert (device["hw"], device["chip"], device["chip_rev"]) == ("esp32", "ESP32-C6FH4 (QFN32)", "0.1")


def test_chip_is_kept_when_an_older_agent_checks_in(client, checkin):
    checkin(chip="ESP32-D0WD-V3", chip_rev="3.1")
    checkin()  # e.g. rolled back to a firmware from before #74
    device = client.get("/api/devices").json()[0]
    assert (device["chip"], device["chip_rev"]) == ("ESP32-D0WD-V3", "3.1")


def test_esp8266_has_no_revision(client, checkin):
    checkin(chip="ESP8266EX")
    device = client.get("/api/devices").json()[0]
    assert (device["chip"], device["chip_rev"]) == ("ESP8266EX", None)


def test_invalid_revision_is_refused(client):
    res = client.post(
        "/api/v1/checkin",
        json={"mac": MAC, "hw": "esp32", "app": "weather", "fw_version": "1.0.0", "chip": "ESP32", "chip_rev": "<b>"},
    )
    assert res.status_code == 422
