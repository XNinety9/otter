/*
 * Minimal Otter device: joins Wi-Fi, starts the agent, then does its "real" job.
 */

#include <stdio.h>

#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_sleep.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"
#include "otter_agent.h"
#include "otter_ca.h"       // generated from OTTER_CA_CERT, see CMakeLists.txt
#include "otter_signing.h"  // generated from OTTER_SIGNING_PUBKEY

#if CONFIG_IDF_TARGET_ESP32C6
#include "led_strip.h"
#endif

#ifndef OTTER_FLEET_KEY
#define OTTER_FLEET_KEY ""
#endif

static const char *TAG = "demo";
static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED BIT0

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED);
        ESP_LOGW(TAG, "Wi-Fi lost, reconnecting");
        esp_wifi_connect();
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

    wifi_config_t cfg = {
        .sta = {.ssid = WIFI_SSID, .password = WIFI_PASS},
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);
}

/* Remote commands (sent from the Otter dashboard) ----------------------------- */

/* "identify": blinks the board's LED for a few seconds, to find the device on a shelf. */
static esp_err_t identify(const char *args, char *result, size_t result_size, void *ctx)
{
#if CONFIG_IDF_TARGET_ESP32C6
    static led_strip_handle_t led; /* the DevKitC-1's RGB LED, on GPIO 8 */
    if (!led) {
        led_strip_config_t strip = {.strip_gpio_num = 8, .max_leds = 1};
        led_strip_rmt_config_t rmt = {.resolution_hz = 10 * 1000 * 1000};
        ESP_RETURN_ON_ERROR(led_strip_new_rmt_device(&strip, &rmt, &led), TAG, "LED init failed");
    }
    for (int i = 0; i < 10; i++) {
        if (i % 2 == 0) {
            led_strip_set_pixel(led, 0, 0, 40, 60);
            led_strip_refresh(led);
        } else {
            led_strip_clear(led);
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    led_strip_clear(led);
#else
    const gpio_num_t pin = GPIO_NUM_2; /* the blue LED of most ESP32 dev boards */
    gpio_reset_pin(pin);
    gpio_set_direction(pin, GPIO_MODE_OUTPUT);
    for (int i = 0; i < 10; i++) {
        gpio_set_level(pin, i % 2 == 0);
        vTaskDelay(pdMS_TO_TICKS(250));
    }
    gpio_set_level(pin, 0);
#endif
    snprintf(result, result_size, "blinked the LED");
    return ESP_OK;
}

/* Remote configuration: try {"alive_interval_s": 10, "greeting": "hello"} on this device or
 * one of its tags in the dashboard. */
static void on_config(const char *config_json, void *ctx)
{
    ESP_LOGI(TAG, "configuration: %s", config_json);
}

/* "echo": answers with its arguments, to try custom commands from the dashboard. */
static esp_err_t echo(const char *args, char *result, size_t result_size, void *ctx)
{
    snprintf(result, result_size, "%s", args);
    return ESP_OK;
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    wifi_connect();

    otter_config_t otter = {
        .server_url = OTTER_SERVER,
#ifdef OTTER_DEMO_SLEEP_S
        .app_name = "otter-sleepy",
#else
        .app_name = "otter-demo",
#endif
        .fleet_key = OTTER_FLEET_KEY[0] ? OTTER_FLEET_KEY : NULL,
#ifdef OTTER_HAS_CA
        .cert_pem = OTTER_CA_PEM,
#endif
#ifdef OTTER_HAS_SIGNING_KEY
        .signing_key_pem = OTTER_SIGNING_PUBKEY_PEM,
#endif
    };
    otter_register_command("identify", identify, NULL);
    otter_register_command("echo", echo, NULL);
    otter_on_config(on_config, NULL);

#ifdef OTTER_DEMO_SLEEP_S
    // A battery device (env esp32c6-sleepy): wake up, do the job, check in once, sleep. Commands,
    // configuration and updates wait for its next wake-up, which Otter knows about.
    if (otter_checkin_once(&otter, OTTER_DEMO_SLEEP_S) != ESP_OK) {
        ESP_LOGW(TAG, "server unreachable, trying again at the next wake-up");
    }
    char greeting[32]; // the saved configuration applies even when the server is unreachable
    otter_config_get_str("greeting", greeting, sizeof(greeting), "measuring");
    ESP_LOGI(TAG, "%s, sleeping %d s", greeting, OTTER_DEMO_SLEEP_S);
    vTaskDelay(pdMS_TO_TICKS(100)); // let the USB console send the last lines
    esp_deep_sleep((uint64_t)OTTER_DEMO_SLEEP_S * 1000000);
#endif

    ESP_ERROR_CHECK(otter_start(&otter));

    // The device's actual work goes here, tuned by its remote configuration.
    while (true) {
        char greeting[32];
        otter_config_get_str("greeting", greeting, sizeof(greeting), "alive");
        ESP_LOGI(TAG, "%s, free heap %lu", greeting, (unsigned long)esp_get_free_heap_size());
        int interval_s = otter_config_get_int("alive_interval_s", 60);
        vTaskDelay(pdMS_TO_TICKS((interval_s > 0 ? interval_s : 60) * 1000));
    }
}
