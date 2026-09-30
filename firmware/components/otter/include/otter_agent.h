#pragma once

/*
 * Otter agent for ESP-IDF: checks in with an Otter server and applies OTA updates.
 * Protocol: docs/protocol.md.
 */

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    /* Base URL without trailing slash, e.g. "http://192.168.1.10:8000". NULL or "": find the
     * server on the LAN over mDNS (_otter._tcp, the server needs OTTER_MDNS=1), again after
     * 3 failed check-ins in a row in case it moved. */
    const char *server_url;
    /* Required. Application name, must match the firmware's app in Otter. */
    const char *app_name;
    /* Optional. Sent as X-Otter-Key when the server requires a fleet key, until the device
     * gets its own token from the server (kept in NVS, namespace "otter"). */
    const char *fleet_key;
    /* Optional. Hardware family, defaults to CONFIG_IDF_TARGET ("esp32", "esp32c3"…). */
    const char *hw;
    /* Optional. Defaults to the app description version (PROJECT_VER). */
    const char *version;
    /* Optional. Server CA certificate (PEM) for https:// URLs. Without it the
     * ESP-IDF certificate bundle is used when enabled. */
    const char *cert_pem;
    /* Optional. Public key (PEM) updates must be signed with: images without a valid
     * signature are refused before the boot partition changes. See "Signed firmware" in the
     * README. Without it, unsigned images are accepted. */
    const char *signing_key_pem;
    /* By default a freshly updated firmware is marked valid as soon as it reaches the
     * server. Set this to call otter_mark_valid() yourself once the app is healthy. */
    bool manual_mark_valid;
    /* A new firmware not marked valid within this delay is rolled back. 0 = 300 s. */
    uint32_t rollback_timeout_s;
} otter_config_t;

/* Starts the agent task. Strings in config must stay valid for the program's lifetime.
 * Networking must be initialized; the agent retries until the server is reachable. */
esp_err_t otter_start(const otter_config_t *config);

/* For devices that sleep between check-ins (deep sleep), instead of otter_start(): checks in
 * once, without long polling, runs the commands, applies the configuration and any pending
 * update (which restarts the device into it), then returns. next_checkin_s is when the device
 * will check in again, so Otter doesn't show it offline meanwhile. A new firmware that can't
 * reach the server for 3 wake-ups in a row is rolled back. Networking must be up. Returns
 * ESP_FAIL when the server couldn't be reached. */
esp_err_t otter_checkin_once(const otter_config_t *config, uint32_t next_checkin_s);

/* Confirms the running firmware so the bootloader won't roll it back. */
esp_err_t otter_mark_valid(void);

/* Handler of a remote command sent from Otter. args is the command's arguments as a JSON
 * object ("{}" without any). Write an optional short message for the dashboard in result
 * and return ESP_OK on success. Runs in the agent's task: keep it short. */
typedef esp_err_t (*otter_command_handler_t)(const char *args, char *result, size_t result_size, void *ctx);

/* Registers, or replaces, the handler of a remote command (up to 8), before or after
 * otter_start(). Built in: "reboot", and "identify", which only logs until the app
 * registers its own (blink an LED, beep…). Names: lowercase letters, digits and _. */
esp_err_t otter_register_command(const char *name, otter_command_handler_t handler, void *ctx);

/* Remote configuration: key/value settings edited in Otter, per tag and per device. The
 * agent keeps the last one in NVS, so it applies from boot even offline. */

/* Called with the configuration (a JSON object, "{}" without any) once the agent has started,
 * then each time it changes in Otter, from the agent's task. Register before or after
 * otter_start(). */
typedef void (*otter_config_handler_t)(const char *config_json, void *ctx);
esp_err_t otter_on_config(otter_config_handler_t handler, void *ctx);

/* The current value of a key, or def when it is missing or of another type. Thread-safe. */
int otter_config_get_int(const char *key, int def);
bool otter_config_get_bool(const char *key, bool def);
/* Copies a string value into buf; returns false (and copies def, if any) when missing. */
bool otter_config_get_str(const char *key, char *buf, size_t size, const char *def);

#ifdef __cplusplus
}
#endif
