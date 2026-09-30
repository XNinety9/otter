#pragma once

/*
 * Improv Wi-Fi over the serial port (https://www.improv-wifi.com/serial/): lets a browser
 * (ESP Web Tools, Home Assistant) or tools/improv.py set the Wi-Fi network of a device
 * that was just flashed, without recompiling it.
 */

#include <stdbool.h>
#include <stddef.h>

typedef struct {
    /* Joins the network (blocking); true once connected. */
    bool (*join)(const char *ssid, const char *password);
    /* Whether the device is on a network now. */
    bool (*connected)(void);
    /* Where to go once provisioned (e.g. the Otter dashboard); NULL or "": nowhere. */
    const char *url;
    const char *firmware_name;
    const char *firmware_version;
    const char *device_name;
} improv_config_t;

/* Starts listening on the serial port(s) in a task. The strings must stay valid. */
void improv_start(const improv_config_t *config);
