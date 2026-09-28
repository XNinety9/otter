# Otter device protocol (v1)

This is the contract between a device agent (ESP-IDF component, Arduino library, simulator…)
and the Otter server. Everything is plain HTTP + JSON; devices never need an open port.

All device endpoints live under `/api/v1`. If the server is configured with a fleet key
(`OTTER_FLEET_KEY`), every device request must send it:

```
X-Otter-Key: <fleet key>
```

## 1. Check-in

The device calls this at boot and then every `checkin_interval_s` seconds.

```
POST /api/v1/checkin
Content-Type: application/json

{
  "mac": "a4:cf:12:34:56:78",     // required, identifies the device
  "hw": "esp32",                  // required, hardware family: esp32, esp32c3, esp8266…
  "app": "weather-station",       // required, firmware application name
  "fw_version": "1.2.0",          // required, currently running version
  "ip": "192.168.1.42",           // optional, server falls back to the TCP peer address
  "rssi": -61,                    // optional, Wi-Fi signal in dBm
  "uptime_s": 3600,               // optional
  "wait_s": 30                    // optional, long polling (see below)
}
```

Response:

```json
{
  "checkin_interval_s": 30,
  "update": null
}
```

or, when an update is scheduled for this device:

```json
{
  "checkin_interval_s": 30,
  "update": {
    "deployment_id": 12,
    "version": "1.3.0",
    "url": "http://otter.local:8000/api/v1/firmwares/4/download",
    "size": 912384,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  }
}
```

### Long polling

With `wait_s` > 0, if no update is scheduled, the server holds the request open for up to
`wait_s` seconds (capped at 60) and answers as soon as a deployment targets the device, so
updates start within a second or two instead of at the next check-in. Set the HTTP timeout
to `wait_s` plus a margin.

Agents should wait `checkin_interval_s` **minus the time the request took** before the next
check-in: against a long-polling server they poll again immediately, against a server
that answers at once they fall back to the plain interval. A device should send
`wait_s: 0` when it needs an immediate answer (e.g. a new firmware waiting to be
marked valid).

## 2. Progress reports

While applying an update, the device reports its progress. Reports are best effort: a lost
report must never abort the update. Sending one every ~5–10 % is plenty.

```
POST /api/v1/deployments/{deployment_id}/progress
Content-Type: application/json

{ "state": "downloading", "progress": 42 }
```

| state         | meaning                                                          |
|---------------|------------------------------------------------------------------|
| `downloading` | image is being streamed to the inactive OTA partition            |
| `rebooting`   | image written and verified, device is about to restart           |
| `failed`      | update aborted; include `"error": "<short reason>"`              |

`progress` is 0–100. A `409 Conflict` answer means the deployment was cancelled (or
superseded) from the UI: the device should abort the update and keep running its current
image. There is no `success` state sent by the device: the update is
confirmed when the device checks in again **reporting the target `fw_version`**. If it
checks in after `rebooting` with any other version, the server marks the deployment as
failed (typically a bootloader rollback).

## 3. Firmware download

```
GET /api/v1/firmwares/{id}/download
```

Returns the raw `.bin` image. The device must verify the SHA-256 of what it wrote
against `update.sha256` before switching boot partitions.

## Agent loop (reference)

```
boot:
    mark running app valid (ESP-IDF rollback)   # once the app is confirmed healthy
loop:
    t0 = now()
    resp = checkin(wait_s = checkin_interval_s)
    if resp.update:
        report(downloading, 0)
        stream url -> OTA partition, report every ~10 %
        verify size + sha256, otherwise report(failed, error) and continue
        report(rebooting, 100)
        restart
    sleep(max(0, resp.checkin_interval_s - (now() - t0)))
```
