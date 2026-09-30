/* Improv Wi-Fi, serial version 1: see improv.h. */

#include "improv.h"

#include <ctype.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"
#if SOC_USB_SERIAL_JTAG_SUPPORTED
/* Polled directly, without the driver: installing it would take the port from the console,
 * whose logs go out there too. */
#include "hal/usb_serial_jtag_ll.h"
#endif

static const char *TAG = "improv";

enum { TYPE_STATE = 1, TYPE_ERROR = 2, TYPE_RPC = 3, TYPE_RPC_RESULT = 4 };
enum { STATE_READY = 2, STATE_PROVISIONING = 3, STATE_PROVISIONED = 4 };
enum { ERROR_NONE = 0, ERROR_INVALID_RPC = 1, ERROR_UNKNOWN_RPC = 2, ERROR_UNABLE_TO_CONNECT = 3 };
enum { RPC_WIFI_SETTINGS = 1, RPC_GET_STATE = 2, RPC_GET_INFO = 3, RPC_SCAN = 4 };

typedef enum { PORT_UART, PORT_USB } port_t;

static improv_config_t s_cfg;
static char s_chip[16]; /* "ESP32-C6": Improv's chip family names */

/* --- Sending ----------------------------------------------------------------- */

static void port_write(port_t port, const uint8_t *data, size_t len)
{
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    if (port == PORT_USB) {
        for (int tries = 0; len && tries < 100; tries++) {
            int n = usb_serial_jtag_ll_write_txfifo(data, len);
            usb_serial_jtag_ll_txfifo_flush();
            data += n;
            len -= n;
            if (len) {
                vTaskDelay(1); /* the host hasn't read the FIFO yet */
            }
        }
        return;
    }
#endif
    uart_write_bytes(CONFIG_ESP_CONSOLE_UART_NUM, data, len);
}

static void send_packet(port_t port, uint8_t type, const uint8_t *data, uint8_t len)
{
    uint8_t packet[6 + 3 + 255 + 2] = {'I', 'M', 'P', 'R', 'O', 'V', 1, type, len};
    memcpy(packet + 9, data, len);
    uint8_t sum = 0;
    for (size_t i = 0; i < 9 + (size_t)len; i++) {
        sum += packet[i];
    }
    packet[9 + len] = sum;
    packet[10 + len] = '\n';
    port_write(port, packet, 11 + len);
}

static void send_state(port_t port, uint8_t state)
{
    send_packet(port, TYPE_STATE, &state, 1);
}

static void send_error(port_t port, uint8_t error)
{
    send_packet(port, TYPE_ERROR, &error, 1);
}

/* An RPC result: the command, then strings, each prefixed with its length. */
static void send_result(port_t port, uint8_t command, const char *const *strings, int count)
{
    uint8_t data[255] = {command, 0};
    size_t len = 2;
    for (int i = 0; i < count; i++) {
        size_t n = strlen(strings[i]);
        if (len + 1 + n > sizeof(data)) {
            break;
        }
        data[len++] = n;
        memcpy(data + len, strings[i], n);
        len += n;
    }
    data[1] = len - 2;
    send_packet(port, TYPE_RPC_RESULT, data, len);
}

static void send_url(port_t port, uint8_t command)
{
    const char *url = s_cfg.url ? s_cfg.url : "";
    send_result(port, command, &url, url[0] ? 1 : 0);
}

/* --- Commands ---------------------------------------------------------------- */

static void wifi_settings(port_t port, const uint8_t *data, uint8_t len)
{
    char ssid[33] = "", password[65] = "";
    uint8_t ssid_len = len > 0 ? data[0] : 0;
    uint8_t pass_len = len > 1 + ssid_len ? data[1 + ssid_len] : 0;
    if (!len || ssid_len >= sizeof(ssid) || pass_len >= sizeof(password) || 2 + ssid_len + pass_len > len) {
        send_error(port, ERROR_INVALID_RPC);
        return;
    }
    memcpy(ssid, data + 1, ssid_len);
    memcpy(password, data + 2 + ssid_len, pass_len);
    ESP_LOGI(TAG, "joining \"%s\" as asked over the serial port", ssid);
    send_state(port, STATE_PROVISIONING);
    if (s_cfg.join(ssid, password)) {
        send_state(port, STATE_PROVISIONED);
        send_url(port, RPC_WIFI_SETTINGS);
    } else {
        send_error(port, ERROR_UNABLE_TO_CONNECT);
        send_state(port, STATE_READY);
    }
}

static void scan(port_t port)
{
    wifi_scan_config_t config = {.show_hidden = false};
    uint16_t count = 20;
    wifi_ap_record_t *aps = calloc(count, sizeof(*aps));
    if (aps && esp_wifi_scan_start(&config, true) == ESP_OK && esp_wifi_scan_get_ap_records(&count, aps) == ESP_OK) {
        for (int i = 0; i < count; i++) {
            bool seen = false; /* one line per network name */
            for (int j = 0; j < i; j++) {
                seen |= strcmp((char *)aps[i].ssid, (char *)aps[j].ssid) == 0;
            }
            if (seen || !aps[i].ssid[0]) {
                continue;
            }
            char rssi[8];
            snprintf(rssi, sizeof(rssi), "%d", aps[i].rssi);
            const char *strings[] = {(char *)aps[i].ssid, rssi, aps[i].authmode == WIFI_AUTH_OPEN ? "NO" : "YES"};
            send_result(port, RPC_SCAN, strings, 3);
        }
    }
    free(aps);
    send_result(port, RPC_SCAN, NULL, 0); /* the end of the list */
}

static void handle_rpc(port_t port, const uint8_t *data, uint8_t len)
{
    if (len < 2 || 2 + data[1] > len) {
        send_error(port, ERROR_INVALID_RPC);
        return;
    }
    switch (data[0]) {
    case RPC_WIFI_SETTINGS:
        wifi_settings(port, data + 2, data[1]);
        break;
    case RPC_GET_STATE:
        if (s_cfg.connected()) {
            send_state(port, STATE_PROVISIONED);
            send_url(port, RPC_GET_STATE);
        } else {
            send_state(port, STATE_READY);
        }
        break;
    case RPC_GET_INFO: {
        const char *strings[] = {s_cfg.firmware_name, s_cfg.firmware_version, s_chip, s_cfg.device_name};
        send_result(port, RPC_GET_INFO, strings, 4);
        break;
    }
    case RPC_SCAN:
        scan(port);
        break;
    default:
        send_error(port, ERROR_UNKNOWN_RPC);
    }
}

/* --- Receiving --------------------------------------------------------------- */

typedef struct {
    uint8_t buf[6 + 3 + 255 + 1];
    size_t len;
} parser_t;

/* Feeds one byte; handles a packet once complete. Anything else (a terminal) is ignored. */
static void feed(parser_t *p, port_t port, uint8_t byte)
{
    static const char header[] = "IMPROV";
    if (p->len < 6 && byte != (uint8_t)header[p->len]) {
        p->len = byte == 'I' ? 1 : 0;
        return;
    }
    p->buf[p->len++] = byte;
    if (p->len < 9 || p->len < 9 + (size_t)p->buf[8] + 1) {
        return;
    }
    uint8_t sum = 0;
    for (size_t i = 0; i < p->len - 1; i++) {
        sum += p->buf[i];
    }
    if (p->buf[6] == 1 && sum == p->buf[p->len - 1] && p->buf[7] == TYPE_RPC) {
        handle_rpc(port, p->buf + 9, p->buf[8]);
    }
    p->len = 0;
}

static void improv_task(void *arg)
{
    parser_t uart = {0};
    uint8_t buf[64];
#if SOC_USB_SERIAL_JTAG_SUPPORTED
    parser_t usb = {0};
#endif
    /* The console writes to this UART too: once the driver is there, it must go through it. */
    bool uart_ok = uart_is_driver_installed(CONFIG_ESP_CONSOLE_UART_NUM);
    if (!uart_ok && uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 256, 1024, 0, NULL, 0) == ESP_OK) {
        uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
        uart_ok = true;
    }
    while (true) {
        bool idle = true;
        if (uart_ok) {
            int n = uart_read_bytes(CONFIG_ESP_CONSOLE_UART_NUM, buf, sizeof(buf), 0);
            for (int i = 0; i < n; i++) {
                feed(&uart, PORT_UART, buf[i]);
            }
            idle &= n <= 0;
        }
#if SOC_USB_SERIAL_JTAG_SUPPORTED
        if (usb_serial_jtag_ll_rxfifo_data_available()) {
            int n = usb_serial_jtag_ll_read_rxfifo(buf, sizeof(buf));
            for (int i = 0; i < n; i++) {
                feed(&usb, PORT_USB, buf[i]);
            }
            idle &= n <= 0;
        }
#endif
        if (idle) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
}

void improv_start(const improv_config_t *config)
{
    s_cfg = *config;
    /* "esp32c6" -> "ESP32-C6" */
    const char *target = CONFIG_IDF_TARGET;
    size_t j = 0;
    for (size_t i = 0; target[i] && j < sizeof(s_chip) - 2; i++) {
        if (i == 5 && strncmp(target, "esp32", 5) == 0) {
            s_chip[j++] = '-';
        }
        s_chip[j++] = toupper((unsigned char)target[i]);
    }
    s_chip[j] = '\0';
    xTaskCreate(improv_task, "improv", 4096, NULL, 4, NULL);
}
