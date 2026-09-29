# Otter device protocol (v1)

This is the contract between a device agent (ESP-IDF component, Arduino library, simulator…)
and the Otter server: JSON requests over HTTP(S), always initiated by the device, so devices
never need an open port. **Use HTTPS**: see [Security](#security) for what plain HTTP exposes.

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
  "ota_slot_size": 1966080,       // optional, bytes available for an update image (see below)
  "free_heap": 284104,            // optional, health: free heap in bytes
  "min_free_heap": 251360,        // optional, lowest free heap since boot
  "reset_reason": "power_on",     // optional, why the device last restarted (see below)
  "boot_count": 17,               // optional, incremented at each boot
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

### OTA slot size

`ota_slot_size` is the room for an update image: the size of the next OTA partition on
ESP-IDF (`esp_ota_get_next_update_partition(NULL)->size`), `ESP.getFreeSketchSpace()` on
Arduino. The server never sends a device an image bigger than that: deploying one to it is
refused, and tag deployments, rollouts and release channels skip it. Without the field, the
server can't check and trusts the device; a check-in without it keeps the last value.

### Health

`reset_reason` is one of `power_on`, `software` (restart, e.g. after an update), `deep_sleep`,
`external` (reset pin), `panic`, `int_watchdog`, `task_watchdog`, `watchdog`, `brownout`,
`power_glitch`, `cpu_lockup`, `usb`, `jtag`, `sdio`, `efuse`, `unknown` (lowercase letters,
digits and `_`). The server flags a device that restarted (a new `boot_count`, or `uptime_s`
going down) because of a panic, watchdog, brownout, power glitch or CPU lockup: highlighted in the UI, counted in `otter_device_crashes_total`
and notified as `device_crashed`.

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

With `failed`, a device may add `"retryable": true|false` to say whether a new attempt could
succeed. Without it, the server guesses from `error`: network problems (`connection lost`,
`download timeout`, `cannot reach…`) are retried, anything else (`sha256 mismatch`, image
validation, not enough space…) is final. A retried deployment goes back to pending and is handed
out again at a later check-in, after 10 s, 20 s… (`OTTER_RETRY_DELAY`), up to
`OTTER_DEPLOY_ATTEMPTS` attempts (default 3).

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

A device can resume an interrupted download with `Range: bytes=<offset>-`: the server answers
`206 Partial Content` with the rest of the image. On a weak Wi-Fi link, a connection that
stops receiving data for a few seconds rarely recovers (the server's TCP waits twice as long
after each loss), so the ESP-IDF agent opens a new one after 8 s of silence and resumes where it
stopped, up to 10 times per attempt, instead of starting over.

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

## Security

What protects what, and what doesn't yet:

| Threat | Plain HTTP | HTTPS (server) | Status |
|---|---|---|---|
| Someone on the network reads the fleet key, device data, or dashboard passwords and cookies | **exposed** | protected | HTTPS: ESP-IDF done, Arduino to do ([#17](https://github.com/XNinety9/otter/issues/17)) |
| Someone on the network (e.g. ARP spoofing) serves a malicious firmware | **exposed**: `sha256` comes through the same channel, it only detects corruption | protected, **if the device verifies the certificate** | HTTPS: ESP-IDF done, Arduino to do (#17) |
| The server itself, or its storage, is compromised and serves a malicious firmware | exposed | exposed | signed firmware: [#18](https://github.com/XNinety9/otter/issues/18) |
| A device's key is extracted from its flash | the shared fleet key opens every device's API | same | per-device tokens: [#15](https://github.com/XNinety9/otter/issues/15) |

Deployment options:

- **Server**: run it behind a TLS reverse proxy. `docker-compose.https.yml` puts Caddy in front of
  Otter, with Let's Encrypt for a public domain or Caddy's own CA on a LAN, and stops publishing
  Otter's plain-HTTP port (see "HTTPS" in the README).
- **ESP-IDF agent**: use an `https://` server URL and give the CA certificate in
  `otter_config_t.cert_pem` (the example takes it from `OTTER_CA_CERT` at build time). Without
  it, the ESP-IDF certificate bundle is used, which covers public certificates such as Let's
  Encrypt but not a private CA. Validated on an ESP32-C6 with Caddy's local CA: check-ins and
  OTA updates over HTTPS, at the same speed as plain HTTP (~220 KB/s).
- **Arduino library**: plain HTTP only for now (#17).

Devices must verify the server's certificate: TLS without verification still lets anyone on
the network impersonate the server.

