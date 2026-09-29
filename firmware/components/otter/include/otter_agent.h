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
    /* Required. Base URL without trailing slash, e.g. "http://192.168.1.10:8000". */
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
    /* By default a freshly updated firmware is marked valid as soon as it reaches the
     * server. Set this to call otter_mark_valid() yourself once the app is healthy. */
    bool manual_mark_valid;
    /* A new firmware not marked valid within this delay is rolled back. 0 = 300 s. */
    uint32_t rollback_timeout_s;
} otter_config_t;

/* Starts the agent task. Strings in config must stay valid for the program's lifetime.
 * Networking must be initialized; the agent retries until the server is reachable. */
esp_err_t otter_start(const otter_config_t *config);

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

#ifdef __cplusplus
}
#endif
