from prometheus_client.parser import text_string_to_metric_families

from test_ota_flow import device_id


def scrape(client) -> dict[tuple, float]:
    """Samples as {(name, sorted label items): value}."""
    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(res.text)
        for s in family.samples
    }


def value(samples, name, **labels) -> float:
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def test_fleet_state(client, checkin, upload):
    checkin(mac="aa:bb:cc:00:00:01")
    checkin(mac="aa:bb:cc:00:00:02", version="1.1.0")
    upload("1.1.0")

    m = scrape(client)
    assert value(m, "otter_devices", app="weather", hw="esp32", version="1.0.0", online="true") == 1
    assert value(m, "otter_devices", app="weather", hw="esp32", version="1.1.0", online="true") == 1
    assert value(m, "otter_firmwares") == 1
    last_seen = [v for (name, _), v in m.items() if name == "otter_device_last_seen_timestamp_seconds"]
    assert len(last_seen) == 2 and all(v > 1.7e9 for v in last_seen)


def test_event_counters(client, checkin, upload):
    before = scrape(client)
    checkin()
    fw1, fw2 = upload("1.1.0"), upload("1.2.0")
    dev = device_id(client)
    client.post("/api/deployments", json={"firmware_id": fw1["id"], "device_ids": [dev]})
    client.post("/api/deployments", json={"firmware_id": fw2["id"], "device_ids": [dev]})  # supersedes
    order = checkin()["update"]
    client.get(order["url"])
    checkin("1.2.0")  # success

    after = scrape(client)

    def delta(name, **labels):
        return value(after, name, **labels) - value(before, name, **labels)

    assert delta("otter_checkins_total", app="weather") == 3
    assert delta("otter_firmware_downloads_total", app="weather", version="1.2.0") == 1
    assert delta("otter_firmware_download_bytes_total") == fw2["size"]
    assert delta("otter_deployment_outcomes_total", status="cancelled") == 1
    assert delta("otter_deployment_outcomes_total", status="success") == 1
    assert value(after, "otter_deployments", status="success") == 1
    assert value(after, "otter_deployments", status="cancelled") == 1
