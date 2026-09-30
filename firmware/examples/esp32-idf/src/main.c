/*
 * An Otter device with ESP-IDF: joins Wi-Fi, starts the Otter agent, then does its job.
 *
 * Start your own firmware from this file. What to adapt is marked [ADAPT 1] to [ADAPT 5];
 * the rest (NVS, Wi-Fi, Improv) can stay as is. See docs/firmware.md for the whole guide,
 * including the other files of this project (CMakeLists.txt, platformio.ini, partitions).
 *
 *   [ADAPT 1] OTTER_APP_NAME   your application's name in Otter
 *   [ADAPT 2] commands         what the dashboard can ask the device to do
 *   [ADAPT 3] on_config()      your settings, edited in the dashboard
 *   [ADAPT 4] otter_config_t   how the agent behaves (validation, rollback…)
 *   [ADAPT 5] the main loop    your device's actual work
 *
 * Build-time settings (Wi-Fi, server, keys) come from the environment: see platformio.ini.
 */

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "driver/gpio.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_sleep.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"
#include "improv.h"
#include "otter_agent.h"
#include "otter_ca.h"       // generated from OTTER_CA_CERT, see CMakeLists.txt
#include "otter_signing.h"  // generated from OTTER_SIGNING_PUBKEY

#include "led_strip.h"

#ifndef OTTER_FLEET_KEY
#define OTTER_FLEET_KEY ""
#endif

static const char *TAG = "demo";

/* [ADAPT 1] Your application's name. Otter matches firmware to devices by application name and
 * hardware: every image you upload for this device must use the same name (tools/push.sh,
 * firmware/apps.json for tools/release.sh). One name per kind of device: "weather-station",
 * "garage-door"… The version comes from CMakeLists.txt. */
#ifdef OTTER_DEMO_SLEEP_S
#define OTTER_APP_NAME "otter-sleepy"
#else
#define OTTER_APP_NAME "otter-demo"
#endif

/* --- Wi-Fi: keep as is ---------------------------------------------------------------------
 * The network comes from WIFI_SSID / WIFI_PASS at build time, else from the one the Wi-Fi
 * driver saved (after Improv or a set_wifi command). Reconnects on its own. */

static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED BIT0
#define WIFI_DISCONNECTED BIT1 /* while joining another network (wifi_join) */
static volatile bool s_joining; /* wifi_join() drives the connection: no automatic reconnection */

/* Whether a network is set: built in (WIFI_SSID), or saved by the Wi-Fi driver after Improv
 * provisioning or a set_wifi command. */
static bool wifi_configured(void)
{
    wifi_config_t cfg = {0};
    return esp_wifi_get_config(WIFI_IF_STA, &cfg) == ESP_OK && cfg.sta.ssid[0];
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (wifi_configured()) {
            esp_wifi_connect();
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED);
        xEventGroupSetBits(s_wifi_events, WIFI_DISCONNECTED);
        if (!s_joining && wifi_configured()) {
            ESP_LOGW(TAG, "Wi-Fi lost, reconnecting");
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED);
    }
}

static void wifi_connect(void)
{
    s_wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (WIFI_SSID[0]) { /* built in; else the network saved earlier, if any */
        wifi_config_t cfg = {
            .sta = {.ssid = WIFI_SSID, .password = WIFI_PASS},
        };
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    }
    ESP_ERROR_CHECK(esp_wifi_start());
}

static bool wifi_connected(void)
{
    return xEventGroupGetBits(s_wifi_events) & WIFI_CONNECTED;
}

/* Connects with cfg, a few attempts within about 20 s. */
static bool wifi_try(wifi_config_t *cfg)
{
    /* The configuration can't change while a connection is in progress: stop first. */
    xEventGroupClearBits(s_wifi_events, WIFI_DISCONNECTED);
    if (esp_wifi_disconnect() == ESP_OK) {
        xEventGroupWaitBits(s_wifi_events, WIFI_DISCONNECTED, pdTRUE, pdTRUE, pdMS_TO_TICKS(3000));
    }
    if (esp_wifi_set_config(WIFI_IF_STA, cfg) != ESP_OK) {
        return false;
    }
    for (int attempt = 0; attempt < 4; attempt++) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED | WIFI_DISCONNECTED);
        esp_wifi_connect();
        EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED | WIFI_DISCONNECTED, pdFALSE, pdFALSE,
                                               pdMS_TO_TICKS(10000));
        if (bits & WIFI_CONNECTED) {
            return true;
        }
    }
    return false;
}

/* Joins another network; back to the previous one if it fails. The driver saves the network
 * in NVS: it is used again at the next boot. */
static bool wifi_join(const char *ssid, const char *password)
{
    wifi_config_t previous = {0}, cfg = {0};
    esp_wifi_get_config(WIFI_IF_STA, &previous);
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", ssid);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", password);
    s_joining = true;
    bool joined = wifi_try(&cfg);
    if (!joined) {
        ESP_LOGW(TAG, "couldn't join \"%s\": back to the previous network", ssid);
        if (previous.sta.ssid[0]) {
            wifi_try(&previous);
        } else {
            esp_wifi_set_config(WIFI_IF_STA, &previous);
        }
    }
    s_joining = false;
    if (!wifi_connected() && wifi_configured()) {
        esp_wifi_connect(); /* keep trying in the background, as usual */
    }
    return joined;
}

/* --- [ADAPT 2] Remote commands ----------------------------------------------------------
 * Commands are sent from the dashboard (device panel, or several devices at once) and run in
 * the agent's task: keep them short. A handler gets its arguments as a JSON object ("{}"
 * without any), writes a short answer for the dashboard in result, and returns ESP_OK or an
 * error. "reboot" is built into the agent; the ones below are examples: keep, change or drop
 * them, and add yours with otter_register_command() in app_main(). */

/* "identify": blinks an LED for a few seconds, to find the device on a shelf. Which one comes
 * from the remote configuration: {"led_gpio": 8, "led_type": "ws2812"} for an addressable RGB
 * LED, or "gpio" for a plain one. The defaults suit the ESP32-C6-DevKitC-1 and most ESP32
 * boards; other boards: try pins from the dashboard, no reflashing needed. */
#if CONFIG_IDF_TARGET_ESP32C6
#define DEFAULT_LED_GPIO 8
#define DEFAULT_LED_TYPE "ws2812"
static const int RESERVED_PINS[] = {12, 13, 16, 17, 24, 25, 26, 27, 28, 29, 30}; /* USB, console, flash */
#elif CONFIG_IDF_TARGET_ESP32
#define DEFAULT_LED_GPIO 2
#define DEFAULT_LED_TYPE "gpio"
static const int RESERVED_PINS[] = {1, 3, 6, 7, 8, 9, 10, 11}; /* console, flash */
#else
#define DEFAULT_LED_GPIO -1 /* unknown board: set led_gpio */
#define DEFAULT_LED_TYPE "gpio"
static const int RESERVED_PINS[] = {-1};
#endif

static led_strip_handle_t s_strip; /* the addressable LED last used, on s_strip_pin */
static int s_strip_pin = -1;

static void release_strip(void)
{
    if (s_strip) {
        led_strip_del(s_strip);
        gpio_reset_pin(s_strip_pin);
        s_strip = NULL;
        s_strip_pin = -1;
    }
}

static esp_err_t blink_ws2812(int pin)
{
    if (s_strip && s_strip_pin != pin) {
        release_strip();
    }
    if (!s_strip) {
        led_strip_config_t strip = {.strip_gpio_num = pin, .max_leds = 1};
        led_strip_rmt_config_t rmt = {.resolution_hz = 10 * 1000 * 1000};
        ESP_RETURN_ON_ERROR(led_strip_new_rmt_device(&strip, &rmt, &s_strip), TAG, "LED init failed");
        s_strip_pin = pin;
    }
    for (int i = 0; i < 10; i++) {
        if (i % 2 == 0) {
            led_strip_set_pixel(s_strip, 0, 0, 80, 160);
            led_strip_refresh(s_strip);
        } else {
            led_strip_clear(s_strip);
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    return led_strip_clear(s_strip);
}

static void blink_gpio(int pin)
{
    if (s_strip_pin == pin) {
        release_strip();
    }
    gpio_reset_pin(pin);
    gpio_set_direction(pin, GPIO_MODE_OUTPUT);
    for (int i = 0; i < 10; i++) {
        gpio_set_level(pin, i % 2 == 0);
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    gpio_reset_pin(pin); /* back to its default state, whatever the LED's polarity */
}

static esp_err_t identify(const char *args, char *result, size_t result_size, void *ctx)
{
    int pin = otter_config_get_int("led_gpio", DEFAULT_LED_GPIO);
    char type[8];
    otter_config_get_str("led_type", type, sizeof(type), DEFAULT_LED_TYPE);
    if (pin < 0 || !GPIO_IS_VALID_OUTPUT_GPIO(pin)) {
        snprintf(result, result_size, "no LED to blink: set led_gpio in the configuration");
        return ESP_ERR_INVALID_ARG;
    }
    for (size_t i = 0; i < sizeof(RESERVED_PINS) / sizeof(RESERVED_PINS[0]); i++) {
        if (RESERVED_PINS[i] == pin) {
            snprintf(result, result_size, "GPIO %d is used by the flash, USB or console: pick another", pin);
            return ESP_ERR_INVALID_ARG;
        }
    }
    if (strcmp(type, "ws2812") == 0) {
        ESP_RETURN_ON_ERROR(blink_ws2812(pin), TAG, "blink failed");
    } else if (strcmp(type, "gpio") == 0) {
        blink_gpio(pin);
    } else {
        snprintf(result, result_size, "led_type must be \"ws2812\" or \"gpio\"");
        return ESP_ERR_INVALID_ARG;
    }
    /* Whether an LED lit up can't be checked from here: say what was tried. */
    snprintf(result, result_size, "blinked GPIO %d as a %s LED", pin, strcmp(type, "gpio") ? "WS2812" : "plain");
    return ESP_OK;
}

/* --- [ADAPT 3] Remote configuration ------------------------------------------------------
 * Settings edited in the dashboard, per device or per tag: a JSON object of strings, numbers
 * and booleans, saved on the device. Read them anywhere with otter_config_get_int/bool/str()
 * (as the main loop below does), and react to changes here: called at start, then each time
 * the configuration changes, without a reboot. Try {"alive_interval_s": 10, "greeting":
 * "hello"} on this device. The keys are yours to choose. */
static void on_config(const char *config_json, void *ctx)
{
    ESP_LOGI(TAG, "configuration: %s", config_json);
}

/* "set_wifi" {"ssid": …, "password": …}: moves the device to another network from the
 * dashboard (moving house, a new router); back to the current one if it can't join it. */
static esp_err_t set_wifi(const char *args, char *result, size_t result_size, void *ctx)
{
    cJSON *root = cJSON_Parse(args);
    cJSON *ssid = cJSON_GetObjectItem(root, "ssid");
    cJSON *password = cJSON_GetObjectItem(root, "password");
    esp_err_t err = ESP_ERR_INVALID_ARG;
    if (!cJSON_IsString(ssid) || !ssid->valuestring[0] || strlen(ssid->valuestring) > 32) {
        snprintf(result, result_size, "args: {\"ssid\": …, \"password\": …}");
    } else if (wifi_join(ssid->valuestring, cJSON_IsString(password) ? password->valuestring : "")) {
        snprintf(result, result_size, "joined %s", ssid->valuestring);
        err = ESP_OK;
    } else {
        snprintf(result, result_size, "couldn't join %s: still on the previous network", ssid->valuestring);
        err = ESP_FAIL;
    }
    cJSON_Delete(root);
    return err;
}

/* "echo": answers with its arguments, to try custom commands from the dashboard. */
static esp_err_t echo(const char *args, char *result, size_t result_size, void *ctx)
{
    snprintf(result, result_size, "%s", args);
    return ESP_OK;
}

void app_main(void)
{
    /* NVS keeps the Wi-Fi network, the device's Otter token and its configuration. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    wifi_connect();

    // Improv Wi-Fi on the serial port: set or change the network from a browser (ESP Web Tools,
    // Home Assistant) or tools/improv.py, without recompiling.
    improv_config_t improv = {
        .join = wifi_join,
        .connected = wifi_connected,
        .url = OTTER_SERVER,
        .firmware_name = OTTER_APP_NAME,
        .firmware_version = esp_app_get_description()->version,
        .device_name = OTTER_APP_NAME,
    };
    improv_start(&improv);
    if (!wifi_configured()) {
        ESP_LOGW(TAG, "no Wi-Fi network set: provision one over the serial port (Improv)");
    }
    xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);

    /* [ADAPT 4] The agent's settings (all fields: components/otter/include/otter_agent.h).
     * Set manual_mark_valid = true to confirm a new firmware yourself, with
     * otter_mark_valid(), once your device works (sensors read, actuators respond): the
     * bootloader rolls back a firmware not confirmed within rollback_timeout_s (300 s). */
    otter_config_t otter = {
        .server_url = OTTER_SERVER, // "" (OTTER_SERVER unset at build time): found over mDNS
        .app_name = OTTER_APP_NAME,
        .fleet_key = OTTER_FLEET_KEY[0] ? OTTER_FLEET_KEY : NULL,
#ifdef OTTER_HAS_CA
        .cert_pem = OTTER_CA_PEM,
#endif
#ifdef OTTER_HAS_SIGNING_KEY
        .signing_key_pem = OTTER_SIGNING_PUBKEY_PEM,
#endif
    };
    /* [ADAPT 2, 3] Your commands and configuration handler (before the agent starts). */
    otter_register_command("identify", identify, NULL);
    otter_register_command("echo", echo, NULL);
    otter_register_command("set_wifi", set_wifi, NULL);
    otter_on_config(on_config, NULL);

#ifdef OTTER_DEMO_SLEEP_S
    // [ADAPT 5, battery devices] Wake up, do the job, check in once, sleep (env esp32c6-sleepy).
    // Commands, configuration and updates wait for the next wake-up, which Otter knows about.
    if (otter_checkin_once(&otter, OTTER_DEMO_SLEEP_S) != ESP_OK) {
        ESP_LOGW(TAG, "server unreachable, trying again at the next wake-up");
    }
    char greeting[32]; // the saved configuration applies even when the server is unreachable
    otter_config_get_str("greeting", greeting, sizeof(greeting), "measuring");
    ESP_LOGI(TAG, "%s, sleeping %d s", greeting, OTTER_DEMO_SLEEP_S);
    vTaskDelay(pdMS_TO_TICKS(100)); // let the USB console send the last lines
    esp_deep_sleep((uint64_t)OTTER_DEMO_SLEEP_S * 1000000);
#endif

    ESP_ERROR_CHECK(otter_start(&otter)); // runs in its own task from now on

    // [ADAPT 5] The device's actual work goes here: read sensors, drive relays… It runs next to
    // the agent, which checks in, applies updates and runs commands on its own. This demo only
    // logs a line, tuned by its remote configuration.
    while (true) {
        char greeting[32];
        otter_config_get_str("greeting", greeting, sizeof(greeting), "alive");
        ESP_LOGI(TAG, "%s, free heap %lu", greeting, (unsigned long)esp_get_free_heap_size());
        int interval_s = otter_config_get_int("alive_interval_s", 60);
        vTaskDelay(pdMS_TO_TICKS((interval_s > 0 ? interval_s : 60) * 1000));
    }
}
