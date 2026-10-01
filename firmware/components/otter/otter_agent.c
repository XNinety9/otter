#include "otter_agent.h"
#include "otter_chip.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_check.h"
#include "esp_idf_version.h"
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
#include "esp_core_dump.h"
#endif
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include "esp_delta_ota.h"
#include "miniz.h" /* tinfl, in ROM */
#include "mdns.h"
#include "mbedtls/pk.h"
#include "nvs.h"
#include "psa/crypto.h"
#include "sdkconfig.h"

#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
#include "esp_crt_bundle.h"
#endif

#define RESPONSE_MAX 4096 /* room for a few commands with arguments */
#define CHUNK_SIZE 4096
#define RETRY_DELAY_S 10
#define HTTP_TIMEOUT_MS 15000
/* A download receiving nothing for this long reconnects and resumes, at most MAX_RESUMES
 * times per attempt. */
#define STALL_TIMEOUT_MS 8000
#define MAX_RESUMES 10
#define MAX_WAIT_S 60

static const char *TAG = "otter";

typedef struct {
    int deployment_id; /* 0 = no update */
    char version[33];
    char url[256];
    int size;
    char sha256[65];
    unsigned char signature[512]; /* DER, decoded from the order's base64 */
    size_t signature_len;         /* 0: unsigned */
    char compressed_url[256];     /* the same image, zlib-compressed ("": none offered) */
    int compressed_size;
    char delta_url[256];          /* a patch from the running image ("": none offered) */
    int delta_size;
    char delta_base_hash[65];     /* the image the patch applies to */
} update_order_t;

static otter_config_t s_cfg;
static uint32_t s_interval_s = 30;
static bool s_pending_verify;
static int32_t s_boot_count = -1; /* -1: unknown (no NVS) */
/* This device's own token, from the server at enrollment (see "Authentication" in
 * docs/protocol.md). Empty: authenticate with the fleet key. */
static char s_token[80];

#define MAX_COMMANDS 8
typedef struct {
    char name[33];
    otter_command_handler_t handler;
    void *ctx;
} command_entry_t;
static command_entry_t s_commands[MAX_COMMANDS];
static bool s_reboot_requested;

/* Remote configuration: the values, their version (as the server named it), the app's handler. */
static SemaphoreHandle_t s_config_lock;
static cJSON *s_config;
static char s_config_version[65];
static otter_config_handler_t s_config_handler;
static void *s_config_ctx;
static bool s_started;
/* One-shot mode (otter_checkin_once, deep sleep): no long polling, next_checkin_s announced. */
static bool s_one_shot;
static uint32_t s_next_checkin_s;
static bool s_checkin_again; /* commands or a configuration came: check in again right away */

/* --- HTTP helpers --------------------------------------------------------- */

static esp_http_client_handle_t new_client(const char *url, esp_http_client_method_t method, int timeout_ms)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .method = method,
        .timeout_ms = timeout_ms,
        .cert_pem = s_cfg.cert_pem,
#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
        .crt_bundle_attach = s_cfg.cert_pem ? NULL : esp_crt_bundle_attach,
#endif
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client && s_token[0]) {
        char bearer[sizeof(s_token) + 8];
        snprintf(bearer, sizeof(bearer), "Bearer %s", s_token);
        esp_http_client_set_header(client, "Authorization", bearer);
    } else if (client && s_cfg.fleet_key) {
        esp_http_client_set_header(client, "X-Otter-Key", s_cfg.fleet_key);
    }
    return client;
}

/* --- Server discovery (mDNS) ---------------------------------------------- */

/* Without a configured server_url, the server advertised on the LAN as _otter._tcp (#19). */
static char s_discovered_url[160];
static int s_failed_checkins; /* in a row: the server may have moved, look for it again */
#define REDISCOVER_AFTER 3

static const char *server_url(void)
{
    return s_cfg.server_url && s_cfg.server_url[0] ? s_cfg.server_url : s_discovered_url;
}

static bool discover_server(void)
{
    static bool mdns_ready;
    if (!mdns_ready) {
        if (mdns_init() != ESP_OK) {
            ESP_LOGE(TAG, "mDNS init failed");
            return false;
        }
        mdns_ready = true;
    }
    mdns_result_t *results = NULL;
    /* Ask for unicast answers first: right after joining the network, multicast answers often
     * don't reach us (access points forward multicast only once they've seen our group join). */
    static const mdns_query_transmission_type_t modes[] = {MDNS_QUERY_UNICAST, MDNS_QUERY_UNICAST,
                                                           MDNS_QUERY_MULTICAST};
    for (int attempt = 0; attempt < 3 && !results; attempt++) {
        if (mdns_query_generic(NULL, "_otter", "_tcp", MDNS_TYPE_PTR, modes[attempt], 2000, 4, &results) != ESP_OK) {
            results = NULL;
        }
    }
    if (!results) {
        ESP_LOGW(TAG, "no Otter server found over mDNS");
        return false;
    }
    s_discovered_url[0] = '\0';
    for (mdns_result_t *r = results; r && !s_discovered_url[0]; r = r->next) {
        /* The server's public URL when it has one (e.g. https:// behind a proxy)... */
        for (size_t i = 0; i < r->txt_count; i++) {
            if (strcmp(r->txt[i].key, "url") == 0 && r->txt[i].value && r->txt[i].value[0]) {
                snprintf(s_discovered_url, sizeof(s_discovered_url), "%s", r->txt[i].value);
            }
        }
        /* ...else its address and port. */
        for (mdns_ip_addr_t *a = r->addr; a && !s_discovered_url[0]; a = a->next) {
            if (a->addr.type == ESP_IPADDR_TYPE_V4) {
                snprintf(s_discovered_url, sizeof(s_discovered_url), "http://" IPSTR ":%u",
                         IP2STR(&a->addr.u_addr.ip4), r->port);
            }
        }
        /* An answer can come without the address record: ask for it. */
        esp_ip4_addr_t ip;
        if (!s_discovered_url[0] && r->hostname && r->port && mdns_query_a(r->hostname, 2000, &ip) == ESP_OK) {
            snprintf(s_discovered_url, sizeof(s_discovered_url), "http://" IPSTR ":%u", IP2STR(&ip), r->port);
        }
    }
    mdns_query_results_free(results);
    if (s_discovered_url[0]) {
        ESP_LOGI(TAG, "server found over mDNS: %s", s_discovered_url);
    } else {
        ESP_LOGW(TAG, "Otter server answered over mDNS without a usable address");
    }
    return s_discovered_url[0] != '\0';
}

/* POSTs a JSON body to server_url + path. Returns the HTTP status, or -1 on network
 * error. The response body is stored in resp when given. With keep, the connection is
 * kept open in *keep for the next call (close it with close_kept()). */
static int post_json(const char *path, const char *body, char *resp, size_t resp_size, int timeout_ms,
                     esp_http_client_handle_t *keep)
{
    char url[256];
    snprintf(url, sizeof(url), "%s%s", server_url(), path);
    bool reused = keep && *keep;
    esp_http_client_handle_t client = reused ? *keep : new_client(url, HTTP_METHOD_POST, timeout_ms);
    if (!client) {
        return -1;
    }
    if (reused) {
        esp_http_client_set_url(client, url);
    } else {
        esp_http_client_set_header(client, "Content-Type", "application/json");
    }

    int status = -1;
    int len = strlen(body);
    if (esp_http_client_open(client, len) == ESP_OK) {
        if (esp_http_client_write(client, body, len) == len && esp_http_client_fetch_headers(client) >= 0) {
            status = esp_http_client_get_status_code(client);
            if (resp) {
                int n = esp_http_client_read_response(client, resp, resp_size - 1);
                resp[n > 0 ? n : 0] = '\0';
            } else {
                esp_http_client_flush_response(client, NULL);
            }
        }
    }
    if (keep && status > 0) {
        *keep = client;
        return status;
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    if (keep) {
        *keep = NULL;
    }
    if (reused) {
        /* The server may have closed the idle connection: once more on a fresh one. */
        return post_json(path, body, resp, resp_size, timeout_ms, keep);
    }
    return status;
}

static void close_kept(esp_http_client_handle_t *keep)
{
    if (*keep) {
        esp_http_client_close(*keep);
        esp_http_client_cleanup(*keep);
        *keep = NULL;
    }
}

/* Progress reports during an update share one connection: over TLS, a new handshake
 * takes about a second, during which the download isn't read and its TCP window closes. */
static esp_http_client_handle_t s_report_conn;

/* Best-effort progress report. Returns false when the server asks to abort (409). */
static bool report(int deployment_id, const char *state, int progress, const char *error)
{
    char path[64];
    snprintf(path, sizeof(path), "/api/v1/deployments/%d/progress", deployment_id);

    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "state", state);
    cJSON_AddNumberToObject(obj, "progress", progress);
    if (error) {
        cJSON_AddStringToObject(obj, "error", error);
    }
    char *body = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);

    int status = post_json(path, body, NULL, 0, HTTP_TIMEOUT_MS, &s_report_conn);
    free(body);
    if (status != 200) {
        ESP_LOGW(TAG, "progress report got HTTP %d", status);
    }
    return status != 409;
}

/* --- Check-in ------------------------------------------------------------- */

static const char *reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_watchdog";
    case ESP_RST_TASK_WDT: return "task_watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    case ESP_RST_USB: return "usb";
    case ESP_RST_JTAG: return "jtag";
    case ESP_RST_EFUSE: return "efuse";
    case ESP_RST_PWR_GLITCH: return "power_glitch";
    case ESP_RST_CPU_LOCKUP: return "cpu_lockup";
    default: return "unknown";
    }
}

/* The token lives in NVS next to the boot counter. */
static void load_token(void)
{
    nvs_handle_t nvs;
    size_t len = sizeof(s_token);
    if (nvs_open("otter", NVS_READONLY, &nvs) == ESP_OK) {
        if (nvs_get_str(nvs, "token", s_token, &len) != ESP_OK) {
            s_token[0] = '\0';
        }
        nvs_close(nvs);
    }
}

static void save_token(const char *token)
{
    snprintf(s_token, sizeof(s_token), "%s", token ? token : "");
    nvs_handle_t nvs;
    if (nvs_open("otter", NVS_READWRITE, &nvs) != ESP_OK) {
        return; /* kept in RAM only: a reboot enrolls again */
    }
    if (s_token[0]) {
        nvs_set_str(nvs, "token", s_token);
    } else {
        nvs_erase_key(nvs, "token");
    }
    nvs_commit(nvs);
    nvs_close(nvs);
}

/* Counts boots in NVS, so the server can tell a restart from a long silence. Needs
 * nvs_flash_init(), which Wi-Fi requires anyway; without it the count isn't sent. */
static void count_boot(void)
{
    nvs_handle_t nvs;
    if (nvs_open("otter", NVS_READWRITE, &nvs) != ESP_OK) {
        return;
    }
    int32_t count = 0;
    nvs_get_i32(nvs, "boots", &count);
    if (nvs_set_i32(nvs, "boots", count + 1) == ESP_OK && nvs_commit(nvs) == ESP_OK) {
        s_boot_count = count + 1;
    }
    nvs_close(nvs);
}

/* Long polling: the server holds the check-in until an update is scheduled for us or
 * this delay elapses, so a deployment reaches the device within a second or two.
 * A firmware awaiting validation must hear back at once instead. */
static uint32_t checkin_wait_s(void)
{
    if (s_pending_verify || s_one_shot) {
        return 0;
    }
    return s_interval_s < MAX_WAIT_S ? s_interval_s : MAX_WAIT_S;
}

static void copy_string(cJSON *obj, const char *key, char *dst, size_t size)
{
    cJSON *item = cJSON_GetObjectItem(obj, key);
    dst[0] = '\0';
    if (cJSON_IsString(item)) {
        strlcpy(dst, item->valuestring, size);
    }
}

static void mac_string(char out[18])
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static char *build_checkin_body(void)
{
    char mac_str[18];
    mac_string(mac_str);

    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "mac", mac_str);
    cJSON_AddStringToObject(obj, "hw", s_cfg.hw);
    char chip_rev[8];
    otter_chip_revision(chip_rev, sizeof(chip_rev));
    cJSON_AddStringToObject(obj, "chip", otter_chip_model());
    cJSON_AddStringToObject(obj, "chip_rev", chip_rev);
    if (otter_flash_size()) {
        cJSON_AddNumberToObject(obj, "flash_size", otter_flash_size());
    }
    if (otter_psram_size()) {
        cJSON_AddNumberToObject(obj, "psram_size", otter_psram_size());
    }
    cJSON_AddStringToObject(obj, "app", s_cfg.app_name);
    cJSON_AddStringToObject(obj, "fw_version", s_cfg.version);
    cJSON_AddNumberToObject(obj, "uptime_s", (double)(esp_timer_get_time() / 1000000));
    cJSON_AddNumberToObject(obj, "free_heap", esp_get_free_heap_size());
    cJSON_AddNumberToObject(obj, "min_free_heap", esp_get_minimum_free_heap_size());
    cJSON_AddStringToObject(obj, "reset_reason", reset_reason());
    if (s_boot_count >= 0) {
        cJSON_AddNumberToObject(obj, "boot_count", s_boot_count);
    }
    cJSON_AddStringToObject(obj, "config_version", s_config_version); /* "": none yet */
    if (s_next_checkin_s) {
        cJSON_AddNumberToObject(obj, "next_checkin_s", s_next_checkin_s);
    }
    const esp_partition_t *slot = esp_ota_get_next_update_partition(NULL);
    if (slot) {
        cJSON_AddNumberToObject(obj, "ota_slot_size", slot->size);
    }
    cJSON_AddNumberToObject(obj, "wait_s", checkin_wait_s());

    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip_info;
    if (netif && esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
        char ip[16];
        snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ip_info.ip));
        cJSON_AddStringToObject(obj, "ip", ip);
    }
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        cJSON_AddNumberToObject(obj, "rssi", ap.rssi);
    }

    char *body = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    return body;
}

/* --- Remote commands ------------------------------------------------------ */

esp_err_t otter_register_command(const char *name, otter_command_handler_t handler, void *ctx)
{
    if (!name || !handler || strlen(name) >= sizeof(s_commands[0].name)) {
        return ESP_ERR_INVALID_ARG;
    }
    command_entry_t *free_slot = NULL;
    for (int i = 0; i < MAX_COMMANDS; i++) {
        if (strcmp(s_commands[i].name, name) == 0) {
            free_slot = &s_commands[i];
            break;
        }
        if (!free_slot && !s_commands[i].name[0]) {
            free_slot = &s_commands[i];
        }
    }
    if (!free_slot) {
        return ESP_ERR_NO_MEM;
    }
    strcpy(free_slot->name, name);
    free_slot->handler = handler;
    free_slot->ctx = ctx;
    return ESP_OK;
}

static esp_err_t builtin_reboot(const char *args, char *result, size_t size, void *ctx)
{
    s_reboot_requested = true; /* once every command is acknowledged */
    snprintf(result, size, "rebooting");
    return ESP_OK;
}

static esp_err_t builtin_identify(const char *args, char *result, size_t size, void *ctx)
{
    ESP_LOGW(TAG, "*** identify requested from Otter ***");
    snprintf(result, size, "logged only: this firmware has no identify handler");
    return ESP_OK;
}

static void run_command(int id, const char *name, const char *args)
{
    char result[128] = "";
    esp_err_t err = ESP_ERR_NOT_SUPPORTED;
    otter_command_handler_t handler = NULL;
    void *ctx = NULL;
    for (int i = 0; i < MAX_COMMANDS; i++) {
        if (strcmp(s_commands[i].name, name) == 0) {
            handler = s_commands[i].handler;
            ctx = s_commands[i].ctx;
        }
    }
    if (!handler && strcmp(name, "reboot") == 0) {
        handler = builtin_reboot;
    } else if (!handler && strcmp(name, "identify") == 0) {
        handler = builtin_identify;
    }
    if (handler) {
        ESP_LOGI(TAG, "command %s %s", name, args);
        err = handler(args, result, sizeof(result), ctx);
    } else {
        snprintf(result, sizeof(result), "unknown command");
    }
    if (err != ESP_OK && !result[0]) {
        snprintf(result, sizeof(result), "%s", esp_err_to_name(err));
    }

    cJSON *ack = cJSON_CreateObject();
    cJSON_AddBoolToObject(ack, "ok", err == ESP_OK);
    if (result[0]) {
        cJSON_AddStringToObject(ack, "message", result);
    }
    char *body = cJSON_PrintUnformatted(ack);
    cJSON_Delete(ack);
    char path[64];
    snprintf(path, sizeof(path), "/api/v1/commands/%d/result", id);
    /* A few tries: right after a command that touches the network (set_wifi…), the first
     * connection often fails; the server can't send the command again (it could run twice). */
    int status = -1;
    for (int attempt = 0; body && attempt < 3 && status < 0; attempt++) {
        if (attempt) {
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
        status = post_json(path, body, NULL, 0, HTTP_TIMEOUT_MS, NULL);
    }
    if (status != 200) {
        ESP_LOGW(TAG, "command %d: result not delivered (HTTP %d)", id, status);
    }
    free(body);
}

static void run_commands(cJSON *commands)
{
    cJSON *command;
    cJSON_ArrayForEach(command, commands) {
        cJSON *id = cJSON_GetObjectItem(command, "id");
        cJSON *name = cJSON_GetObjectItem(command, "name");
        if (!cJSON_IsNumber(id) || !cJSON_IsString(name)) {
            continue;
        }
        cJSON *args = cJSON_GetObjectItem(command, "args");
        char *args_json = cJSON_IsObject(args) ? cJSON_PrintUnformatted(args) : NULL;
        run_command(id->valueint, name->valuestring, args_json ? args_json : "{}");
        free(args_json);
        s_checkin_again = true;
    }
}

/* --- Remote configuration -------------------------------------------------- */

static void notify_config(void)
{
    if (!s_config_handler) {
        return;
    }
    xSemaphoreTake(s_config_lock, portMAX_DELAY);
    char *json = s_config ? cJSON_PrintUnformatted(s_config) : NULL;
    xSemaphoreGive(s_config_lock);
    s_config_handler(json ? json : "{}", s_config_ctx);
    free(json);
}

/* Replaces the configuration; save: also write it to NVS. Takes ownership of values. */
static void set_config(cJSON *values, const char *version, bool save)
{
    xSemaphoreTake(s_config_lock, portMAX_DELAY);
    cJSON_Delete(s_config);
    s_config = values;
    snprintf(s_config_version, sizeof(s_config_version), "%s", version);
    char *json = save && values ? cJSON_PrintUnformatted(values) : NULL;
    xSemaphoreGive(s_config_lock);

    nvs_handle_t nvs;
    if (save && nvs_open("otter", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_str(nvs, "cfg", json ? json : "{}");
        nvs_set_str(nvs, "cfg_ver", version);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    free(json);
}

static void load_config(void)
{
    nvs_handle_t nvs;
    if (nvs_open("otter", NVS_READONLY, &nvs) != ESP_OK) {
        return;
    }
    char version[sizeof(s_config_version)];
    size_t version_len = sizeof(version), json_len = 0;
    if (nvs_get_str(nvs, "cfg_ver", version, &version_len) == ESP_OK &&
        nvs_get_str(nvs, "cfg", NULL, &json_len) == ESP_OK) {
        char *json = malloc(json_len);
        if (json && nvs_get_str(nvs, "cfg", json, &json_len) == ESP_OK) {
            cJSON *values = cJSON_Parse(json);
            if (cJSON_IsObject(values)) {
                set_config(values, version, false);
            } else {
                cJSON_Delete(values);
            }
        }
        free(json);
    }
    nvs_close(nvs);
}

static void apply_config_order(cJSON *config)
{
    cJSON *version = cJSON_GetObjectItem(config, "version");
    cJSON *values = cJSON_GetObjectItem(config, "values");
    if (!cJSON_IsString(version) || !cJSON_IsObject(values)) {
        ESP_LOGE(TAG, "malformed configuration");
        return;
    }
    cJSON *copy = cJSON_Duplicate(values, true);
    set_config(copy, version->valuestring, true);
    ESP_LOGI(TAG, "configuration %s received", version->valuestring[0] ? version->valuestring : "(empty)");
    notify_config();
    s_checkin_again = true; /* confirm the new version right away: the dashboard shows it in sync */
}

esp_err_t otter_on_config(otter_config_handler_t handler, void *ctx)
{
    s_config_handler = handler;
    s_config_ctx = ctx;
    if (s_started) {
        notify_config();
    }
    return ESP_OK;
}

static cJSON *config_item(const char *key)
{
    return s_config ? cJSON_GetObjectItem(s_config, key) : NULL;
}

int otter_config_get_int(const char *key, int def)
{
    if (!s_config_lock) {
        return def;
    }
    xSemaphoreTake(s_config_lock, portMAX_DELAY);
    cJSON *item = config_item(key);
    int value = cJSON_IsNumber(item) ? item->valueint : def;
    xSemaphoreGive(s_config_lock);
    return value;
}

bool otter_config_get_bool(const char *key, bool def)
{
    if (!s_config_lock) {
        return def;
    }
    xSemaphoreTake(s_config_lock, portMAX_DELAY);
    cJSON *item = config_item(key);
    bool value = cJSON_IsBool(item) ? cJSON_IsTrue(item) : def;
    xSemaphoreGive(s_config_lock);
    return value;
}

bool otter_config_get_str(const char *key, char *buf, size_t size, const char *def)
{
    bool found = false;
    if (s_config_lock) {
        xSemaphoreTake(s_config_lock, portMAX_DELAY);
        cJSON *item = config_item(key);
        if (cJSON_IsString(item)) {
            snprintf(buf, size, "%s", item->valuestring);
            found = true;
        }
        xSemaphoreGive(s_config_lock);
    }
    if (!found && def) {
        snprintf(buf, size, "%s", def);
    }
    return found;
}

/* --- Crash reports ---------------------------------------------------------- */

/* ELF is the only core dump format from ESP-IDF 6; the summary needs it. */
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH && (ESP_IDF_VERSION_MAJOR >= 6 || CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF)
/* After a crash, sends the core dump's summary to Otter, which decodes it with the firmware's
 * ELF file (#24), then erases it. Once per boot, after the first check-in that reached it. */
static void report_crash(void)
{
    static bool done;
    if (done) {
        return;
    }
    done = true;
    if (esp_core_dump_image_check() != ESP_OK) {
        return; /* no core dump (or an unreadable one) */
    }
    esp_core_dump_summary_t *summary = calloc(1, sizeof(*summary));
    if (!summary || esp_core_dump_get_summary(summary) != ESP_OK) {
        free(summary);
        esp_core_dump_image_erase();
        return;
    }
    cJSON *obj = cJSON_CreateObject();
    cJSON_AddStringToObject(obj, "elf_sha256", (const char *)summary->app_elf_sha256);
    cJSON_AddStringToObject(obj, "task", summary->exc_task);
    cJSON_AddNumberToObject(obj, "pc", summary->exc_pc);
    char reason[160];
    if (esp_core_dump_get_panic_reason(reason, sizeof(reason)) == ESP_OK) {
        cJSON_AddStringToObject(obj, "reason", reason);
    }
#if CONFIG_IDF_TARGET_ARCH_XTENSA
    cJSON *bt = cJSON_AddArrayToObject(obj, "backtrace");
    for (uint32_t i = 0; i < summary->exc_bt_info.depth; i++) {
        cJSON_AddItemToArray(bt, cJSON_CreateNumber(summary->exc_bt_info.bt[i]));
    }
    cJSON_AddBoolToObject(obj, "backtrace_corrupted", summary->exc_bt_info.corrupted);
#else
    /* RISC-V: no unwinding on the device; the server finds the callers in the stack words. */
    cJSON *regs = cJSON_AddObjectToObject(obj, "registers");
    cJSON_AddNumberToObject(regs, "ra", summary->ex_info.ra);
    cJSON_AddNumberToObject(regs, "sp", summary->ex_info.sp);
    cJSON_AddNumberToObject(regs, "mcause", summary->ex_info.mcause);
    cJSON_AddNumberToObject(regs, "mtval", summary->ex_info.mtval);
    cJSON *stack = cJSON_AddArrayToObject(obj, "stack");
    const uint32_t *words = (const uint32_t *)summary->exc_bt_info.stackdump;
    for (uint32_t i = 0; i < summary->exc_bt_info.dump_size / 4; i++) {
        cJSON_AddItemToArray(stack, cJSON_CreateNumber(words[i]));
    }
#endif
    free(summary);
    char *body = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);

    char path[64] = "/api/v1/crashes";
    if (!s_token[0]) { /* with the fleet key, name the device */
        char mac[18];
        mac_string(mac);
        snprintf(path, sizeof(path), "/api/v1/crashes?mac=%s", mac);
    }
    int status = body ? post_json(path, body, NULL, 0, HTTP_TIMEOUT_MS, NULL) : -1;
    free(body);
    if (status == 200 || status == 404 || status == 422) {
        esp_core_dump_image_erase(); /* delivered, or never will be */
        ESP_LOGI(TAG, "crash report sent (HTTP %d)", status);
    } else {
        done = false; /* try again at the next check-in */
    }
}
#else
static void report_crash(void) {}
#endif

/* --- Check-in (continued) ------------------------------------------------- */

/* Returns true when the server answered. Fills order when an update is scheduled. */
static bool checkin(update_order_t *order)
{
    order->deployment_id = 0;
    if (!server_url()[0] && !discover_server()) {
        return false;
    }
    char *body = build_checkin_body();
    char *resp = malloc(RESPONSE_MAX);
    if (!body || !resp) {
        free(body);
        free(resp);
        return false;
    }

    int status = post_json("/api/v1/checkin", body, resp, RESPONSE_MAX, checkin_wait_s() * 1000 + HTTP_TIMEOUT_MS, NULL);
    free(body);

    bool reached = false;
    if (status == 200) {
        s_failed_checkins = 0;
    } else if (status < 0 && !(s_cfg.server_url && s_cfg.server_url[0]) && ++s_failed_checkins >= REDISCOVER_AFTER) {
        s_failed_checkins = 0;
        s_discovered_url[0] = '\0'; /* look for the server again at the next check-in */
    }
    cJSON *root = status == 200 ? cJSON_Parse(resp) : NULL;
    if (root) {
        reached = true;
        cJSON *interval = cJSON_GetObjectItem(root, "checkin_interval_s");
        if (cJSON_IsNumber(interval) && interval->valueint > 0) {
            s_interval_s = interval->valueint;
        }
        cJSON *token = cJSON_GetObjectItem(root, "token");
        if (cJSON_IsString(token) && strlen(token->valuestring) < sizeof(s_token)) {
            save_token(token->valuestring);
            ESP_LOGI(TAG, "enrolled: this device now has its own token");
        }
        cJSON *config = cJSON_GetObjectItem(root, "config");
        if (cJSON_IsObject(config)) {
            apply_config_order(config);
        }
        cJSON *commands = cJSON_GetObjectItem(root, "commands");
        if (cJSON_IsArray(commands)) {
            run_commands(commands);
        }
        cJSON *update = cJSON_GetObjectItem(root, "update");
        if (cJSON_IsObject(update)) {
            cJSON *id = cJSON_GetObjectItem(update, "deployment_id");
            cJSON *size = cJSON_GetObjectItem(update, "size");
            copy_string(update, "version", order->version, sizeof(order->version));
            copy_string(update, "url", order->url, sizeof(order->url));
            copy_string(update, "sha256", order->sha256, sizeof(order->sha256));
            cJSON *compressed = cJSON_GetObjectItem(update, "compressed");
            cJSON *compressed_size = cJSON_GetObjectItem(compressed, "size");
            cJSON *format = cJSON_GetObjectItem(compressed, "format");
            order->compressed_url[0] = '\0';
            order->compressed_size = 0;
            if (cJSON_IsNumber(compressed_size) && cJSON_IsString(format) && strcmp(format->valuestring, "zlib") == 0) {
                copy_string(compressed, "url", order->compressed_url, sizeof(order->compressed_url));
                order->compressed_size = compressed_size->valueint;
            }
            cJSON *delta = cJSON_GetObjectItem(update, "delta");
            cJSON *delta_size = cJSON_GetObjectItem(delta, "size");
            cJSON *delta_format = cJSON_GetObjectItem(delta, "format");
            order->delta_url[0] = '\0';
            order->delta_size = 0;
            if (cJSON_IsNumber(delta_size) && cJSON_IsString(delta_format) &&
                strcmp(delta_format->valuestring, "detools-heatshrink") == 0) {
                copy_string(delta, "url", order->delta_url, sizeof(order->delta_url));
                copy_string(delta, "base_hash", order->delta_base_hash, sizeof(order->delta_base_hash));
                order->delta_size = delta_size->valueint;
            }
            cJSON *signature = cJSON_GetObjectItem(update, "signature");
            order->signature_len = 0;
            if (cJSON_IsString(signature) &&
                mbedtls_base64_decode(order->signature, sizeof(order->signature), &order->signature_len,
                                      (const unsigned char *)signature->valuestring,
                                      strlen(signature->valuestring)) != 0) {
                order->signature_len = 0; /* malformed: treated as unsigned */
            }
            if (cJSON_IsNumber(id) && cJSON_IsNumber(size) && order->url[0] && strlen(order->sha256) == 64) {
                order->deployment_id = id->valueint;
                order->size = size->valueint;
            } else {
                ESP_LOGE(TAG, "malformed update order");
            }
        }
        cJSON_Delete(root);
    } else if (status == 401 && s_token[0]) {
        /* Re-enrolled in Otter (or its database was reset): enroll again with the fleet key. */
        ESP_LOGW(TAG, "token refused, enrolling again with the fleet key");
        save_token(NULL);
    } else if (status == 403) {
        ESP_LOGW(TAG, "check-in refused: %s", resp); /* revoked, or awaiting approval */
    } else {
        ESP_LOGW(TAG, "check-in failed (HTTP %d)", status);
    }
    free(resp);
    return reached;
}

/* --- OTA ------------------------------------------------------------------ */

/* Opens the firmware URL, from byte offset on when resuming. Returns NULL on success,
 * or the error to report. */
static const char *open_download(const char *url, int offset, esp_http_client_handle_t *out)
{
    /* One timeout for everything: over TLS, reads wait on the socket timeout set here. */
    esp_http_client_handle_t client = new_client(url, HTTP_METHOD_GET, STALL_TIMEOUT_MS);
    if (!client) {
        return "out of memory";
    }
    if (offset > 0) {
        char range[32];
        snprintf(range, sizeof(range), "bytes=%d-", offset);
        esp_http_client_set_header(client, "Range", range);
    }
    const char *err = NULL;
    if (esp_http_client_open(client, 0) != ESP_OK || esp_http_client_fetch_headers(client) < 0) {
        err = "cannot reach firmware URL";
    } else if (esp_http_client_get_status_code(client) != (offset > 0 ? 206 : 200)) {
        err = "firmware download refused";
    }
    if (err) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return err;
    }
    *out = client;
    return NULL;
}

/* Checks the image's signature (see "Signed firmware" in the README). NULL when valid. */
static const char *verify_signature(const uint8_t digest[32], const update_order_t *order)
{
    mbedtls_pk_context key;
    mbedtls_pk_init(&key);
    const char *err = NULL;
    if (mbedtls_pk_parse_public_key(&key, (const unsigned char *)s_cfg.signing_key_pem,
                                    strlen(s_cfg.signing_key_pem) + 1) != 0) {
        err = "firmware signing key unreadable"; /* a mistake in this firmware's config */
    } else if (mbedtls_pk_verify(&key, MBEDTLS_MD_SHA256, digest, 32, order->signature, order->signature_len) != 0) {
        err = "invalid signature: not signed with this device's key";
    }
    mbedtls_pk_free(&key);
    return err;
}

/* Where downloaded bytes go: inflated first when the transfer is compressed (#25), then
 * written to the OTA slot and hashed. */
typedef struct {
    esp_ota_handle_t ota;
    psa_hash_operation_t *sha;
    int written;    /* image bytes written */
    int image_size; /* as announced */
    tinfl_decompressor *inflator; /* NULL: the transfer is the plain image */
    uint8_t *dict;                /* TINFL_LZ_DICT_SIZE bytes: the inflate window */
    size_t dict_ofs;
    bool inflated;                /* the compressed stream ended */
    esp_delta_ota_handle_t delta; /* the transfer is a patch against the running image (#26) */
    const esp_partition_t *running;
    const char *delta_err;        /* why writing the patched image failed */
} sink_t;

static const char *sink_image(sink_t *s, const uint8_t *data, size_t n)
{
    if (s->written + (int)n > s->image_size) {
        return "image larger than announced";
    }
    if (esp_ota_write(s->ota, data, n) != ESP_OK) {
        return "flash write failed";
    }
    psa_hash_update(s->sha, data, n);
    s->written += n;
    return NULL;
}

/* The patch reads the running image and writes the new one. */
static esp_err_t delta_read(uint8_t *buf, size_t size, int offset, void *sink)
{
    return esp_partition_read(((sink_t *)sink)->running, offset, buf, size);
}

static esp_err_t delta_write(const uint8_t *buf, size_t size, void *sink)
{
    sink_t *s = sink;
    s->delta_err = sink_image(s, buf, size);
    return s->delta_err ? ESP_FAIL : ESP_OK;
}

/* Whether the device runs the image a patch applies to. */
static bool runs_image(const char *image_hash)
{
    uint8_t digest[32];
    char hex[65];
    if (esp_partition_get_sha256(esp_ota_get_running_partition(), digest) != ESP_OK) {
        return false;
    }
    for (int i = 0; i < 32; i++) {
        sprintf(&hex[i * 2], "%02x", digest[i]);
    }
    return strcasecmp(hex, image_hash) == 0;
}

/* more: bytes of the transfer are still to come after these. */
static const char *sink_transfer(sink_t *s, const uint8_t *in, size_t n, bool more)
{
    if (s->delta) {
        if (esp_delta_ota_feed_patch(s->delta, in, n) != ESP_OK) {
            return s->delta_err ? s->delta_err : "delta patch failed";
        }
        return NULL;
    }
    if (!s->inflator) {
        return sink_image(s, in, n);
    }
    while (true) {
        size_t in_bytes = n, out_bytes = TINFL_LZ_DICT_SIZE - s->dict_ofs;
        tinfl_status status = tinfl_decompress(s->inflator, in, &in_bytes, s->dict, s->dict + s->dict_ofs, &out_bytes,
                                               TINFL_FLAG_PARSE_ZLIB_HEADER | (more ? TINFL_FLAG_HAS_MORE_INPUT : 0));
        in += in_bytes;
        n -= in_bytes;
        if (out_bytes) {
            const char *err = sink_image(s, s->dict + s->dict_ofs, out_bytes);
            if (err) {
                return err;
            }
            s->dict_ofs = (s->dict_ofs + out_bytes) & (TINFL_LZ_DICT_SIZE - 1);
        }
        if (status < TINFL_STATUS_DONE) {
            return "decompression failed";
        }
        if (status == TINFL_STATUS_DONE) {
            s->inflated = true;
            return n ? "data after the compressed image" : NULL;
        }
        if (status == TINFL_STATUS_NEEDS_MORE_INPUT && n == 0) {
            return more ? NULL : "compressed image truncated";
        }
    }
}

static void apply_update(const update_order_t *order)
{
    const int dep = order->deployment_id;
    ESP_LOGI(TAG, "updating %s -> %s (%d bytes)", s_cfg.version, order->version, order->size);
    if (s_cfg.signing_key_pem && !order->signature_len) {
        /* No need to download it: it would be refused anyway. */
        ESP_LOGE(TAG, "update failed: unsigned firmware refused");
        report(dep, "failed", 0, "unsigned firmware refused: this device only accepts signed images");
        close_kept(&s_report_conn);
        return;
    }
    if (!report(dep, "downloading", 0, NULL)) {
        ESP_LOGW(TAG, "update cancelled by server");
        close_kept(&s_report_conn);
        return;
    }

    const char *err = NULL;
    bool cancelled = false;
    esp_http_client_handle_t client = NULL;
    esp_ota_handle_t ota = 0;
    int received = 0, last_reported = 0;
    uint8_t digest[32];
    size_t digest_len = 0;
    char digest_hex[65];
    psa_hash_operation_t sha = PSA_HASH_OPERATION_INIT;
    wifi_ps_type_t saved_ps = WIFI_PS_NONE;
    bool ps_changed = false;

    /* The smallest transfer offered: a patch from the running image (if it really runs the
     * patch's base), else the compressed image, else the plain one. */
    bool patched = order->delta_url[0] && order->delta_size > 0;
    if (patched && !runs_image(order->delta_base_hash)) {
        ESP_LOGW(TAG, "the patch offered is for another image than the running one: not using it");
        patched = false;
    }
    const bool compressed = !patched && order->compressed_url[0] && order->compressed_size > 0;
    const char *url = patched ? order->delta_url : compressed ? order->compressed_url : order->url;
    const int transfer_size = patched ? order->delta_size : compressed ? order->compressed_size : order->size;
    sink_t sink = {.sha = &sha, .image_size = order->size, .running = esp_ota_get_running_partition()};
    if (patched) {
        esp_delta_ota_cfg_t cfg = {
            .user_data = &sink,
            .read_cb_with_user_data = delta_read,
            .write_cb_with_user_data = delta_write,
        };
        sink.delta = esp_delta_ota_init(&cfg);
    }

    const esp_partition_t *partition = esp_ota_get_next_update_partition(NULL);
    char *buf = malloc(CHUNK_SIZE);
    if (compressed) {
        sink.inflator = malloc(sizeof(tinfl_decompressor));
        sink.dict = malloc(TINFL_LZ_DICT_SIZE);
        if (sink.inflator) {
            tinfl_init(sink.inflator);
        }
    }
    if (!partition) {
        err = "no OTA partition";
        goto done;
    }
    if (!buf || (compressed && (!sink.inflator || !sink.dict)) || (patched && !sink.delta)) {
        err = "out of memory";
        goto done;
    }
    if (psa_crypto_init() != PSA_SUCCESS || psa_hash_setup(&sha, PSA_ALG_SHA_256) != PSA_SUCCESS) {
        err = "sha256 init failed";
        goto done;
    }

    /* Erase the slot before connecting: a multi-second erase with a connection open
     * stalls the transfer and, on a weak link, makes the server's TCP back off. */
    int64_t t_start = esp_timer_get_time();
    if (esp_ota_begin(partition, order->size, &ota) != ESP_OK) {
        ota = 0;
        err = "esp_ota_begin failed (image too big?)";
        goto done;
    }
    sink.ota = ota;
    int64_t t_connect = esp_timer_get_time();

    /* Modem sleep caps throughput to what fits between DTIM beacons: keep the radio awake. */
    if (esp_wifi_get_ps(&saved_ps) == ESP_OK && saved_ps != WIFI_PS_NONE) {
        ps_changed = esp_wifi_set_ps(WIFI_PS_NONE) == ESP_OK;
    }

    ESP_LOGI(TAG, "flash erase took %lld ms%s", (t_connect - t_start) / 1000,
             patched ? ", downloading a patch" : compressed ? ", downloading it compressed" : "");

    int64_t t_slice = 0, t_last_read = 0, max_gap = 0;
    int slice_start = 0, connections = 0;

    while (received < transfer_size) {
        if (!client) {
            /* On a lossy link a stalled connection rarely recovers, as the server's TCP
             * waits twice as long after each loss: a new one starts afresh, from where we are. */
            if (connections++ > MAX_RESUMES) {
                if (!err) { /* else the last connection couldn't even open: report why */
                    err = "connection lost";
                }
                goto done;
            }
            if (connections > 1) {
                ESP_LOGW(TAG, "download stalled at %d bytes, resuming (%d/%d)", received, connections - 1,
                         MAX_RESUMES);
            }
            err = open_download(url, received, &client);
            if (err && strcmp(err, "firmware download refused") == 0) {
                goto done;
            } else if (err) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
            if (connections == 1) {
                ESP_LOGI(TAG, "headers after %lld ms", (esp_timer_get_time() - t_connect) / 1000);
                t_slice = esp_timer_get_time();
            }
            t_last_read = esp_timer_get_time();
        }
        int n = esp_http_client_read(client, buf, CHUNK_SIZE);
        if (n <= 0) {
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            client = NULL;
            continue;
        }
        if (received + n > transfer_size) {
            err = "image larger than announced";
            goto done;
        }
        received += n;
        if ((err = sink_transfer(&sink, (const uint8_t *)buf, n, received < transfer_size)) != NULL) {
            goto done;
        }
        int64_t now = esp_timer_get_time();
        if (now - t_last_read > max_gap) {
            max_gap = now - t_last_read;
        }
        t_last_read = now;

        int pct = (int)((int64_t)received * 100 / transfer_size);
        if (pct - last_reported >= 10 && received < transfer_size) {
            last_reported = pct;
            int64_t slice_ms = (now - t_slice) / 1000;
            bool keep_going = report(dep, "downloading", pct, NULL);
            int64_t after_report = esp_timer_get_time();
            ESP_LOGI(TAG, "%3d%%  slice %5lld ms  %4lld KB/s  max read gap %5lld ms  report %4lld ms", pct, slice_ms,
                     slice_ms ? (int64_t)(received - slice_start) * 1000 / 1024 / slice_ms : 0, max_gap / 1000,
                     (after_report - now) / 1000);
            if (!keep_going) {
                cancelled = true;
                goto done;
            }
            t_slice = t_last_read = after_report;
            slice_start = received;
            max_gap = 0;
        }
    }

    ESP_LOGI(TAG, "download done: %d bytes in %lld ms", received, (esp_timer_get_time() - t_start) / 1000);
    if (sink.delta && esp_delta_ota_finalize(sink.delta) != ESP_OK) {
        err = sink.delta_err ? sink.delta_err : "delta patch failed";
        goto done;
    }
    if (sink.written != order->size) {
        err = compressed && !sink.inflated ? "compressed image truncated" : "image smaller than announced";
        goto done;
    }
    if (psa_hash_finish(&sha, digest, sizeof(digest), &digest_len) != PSA_SUCCESS) {
        err = "sha256 failed";
        goto done;
    }
    for (int i = 0; i < 32; i++) {
        sprintf(&digest_hex[i * 2], "%02x", digest[i]);
    }
    if (strcasecmp(digest_hex, order->sha256) != 0) {
        err = "sha256 mismatch";
        goto done;
    }
    if (s_cfg.signing_key_pem && (err = verify_signature(digest, order)) != NULL) {
        goto done;
    }

    esp_err_t end_err = esp_ota_end(ota); /* also validates the image; frees the handle */
    ota = 0;
    if (end_err != ESP_OK) {
        err = end_err == ESP_ERR_OTA_VALIDATE_FAILED ? "image validation failed" : "esp_ota_end failed";
        goto done;
    }
    if (esp_ota_set_boot_partition(partition) != ESP_OK) {
        err = "cannot set boot partition";
        goto done;
    }

done:
    if (ota) {
        esp_ota_abort(ota);
    }
    if (client) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
    }
    free(buf);
    free(sink.inflator);
    free(sink.dict);
    if (sink.delta) {
        esp_delta_ota_deinit(sink.delta);
    }
    psa_hash_abort(&sha);
    if (ps_changed) {
        esp_wifi_set_ps(saved_ps);
    }

    if (cancelled) {
        ESP_LOGW(TAG, "update cancelled by server");
    } else if (err) {
        ESP_LOGE(TAG, "update failed: %s", err);
        report(dep, "failed", 0, err);
    } else {
        report(dep, "rebooting", 100, NULL);
    }
    close_kept(&s_report_conn);
    if (cancelled || err) {
        return;
    }

    ESP_LOGI(TAG, "update written, rebooting into %s", order->version);
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

/* --- Agent task ----------------------------------------------------------- */

static void otter_task(void *arg)
{
    update_order_t order;

    while (true) {
        int64_t started = esp_timer_get_time();
        bool reached = checkin(&order);
        if (reached) {
            report_crash();
        }

        if (s_pending_verify) {
            if (reached && !s_cfg.manual_mark_valid) {
                otter_mark_valid();
            } else if (esp_timer_get_time() / 1000000 > s_cfg.rollback_timeout_s) {
                ESP_LOGE(TAG, "firmware not validated after %" PRIu32 " s, rolling back", s_cfg.rollback_timeout_s);
                esp_ota_mark_app_invalid_rollback_and_reboot();
            }
        }

        if (s_reboot_requested) {
            ESP_LOGW(TAG, "rebooting, as asked from Otter");
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }
        if (order.deployment_id) {
            apply_update(&order);
            continue; /* only reached on failure/cancel: check in again right away */
        }
        if (!reached) {
            vTaskDelay(pdMS_TO_TICKS(RETRY_DELAY_S * 1000));
            continue;
        }
        if (s_checkin_again) {
            s_checkin_again = false;
            continue;
        }
        /* A long-polling server already made us wait: poll again right away. A server
         * without long polling answers at once, and we fall back to the plain interval. */
        int64_t elapsed_ms = (esp_timer_get_time() - started) / 1000;
        int64_t remaining_ms = (int64_t)s_interval_s * 1000 - elapsed_ms;
        if (remaining_ms > 0) {
            vTaskDelay(pdMS_TO_TICKS(remaining_ms));
        }
    }
}

esp_err_t otter_mark_valid(void)
{
    if (!s_pending_verify) {
        return ESP_OK;
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        s_pending_verify = false;
        ESP_LOGI(TAG, "firmware %s marked valid", s_cfg.version);
    }
    return err;
}

/* Common to otter_start() and otter_checkin_once(). */
static esp_err_t init(const otter_config_t *config)
{
    ESP_RETURN_ON_FALSE(config && config->app_name, ESP_ERR_INVALID_ARG, TAG, "app_name is required");
    ESP_RETURN_ON_FALSE(!s_started, ESP_ERR_INVALID_STATE, TAG, "the agent is already started");
    s_cfg = *config;
    if (!s_cfg.hw) {
        s_cfg.hw = CONFIG_IDF_TARGET;
    }
    if (!s_cfg.version) {
        s_cfg.version = esp_app_get_description()->version;
    }
    count_boot();
    load_token();
    s_config_lock = xSemaphoreCreateMutex();
    load_config();
    if (!s_cfg.rollback_timeout_s) {
        s_cfg.rollback_timeout_s = 300;
    }

    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        s_pending_verify = true;
        ESP_LOGW(TAG, "running a new firmware (%s), pending validation", s_cfg.version);
    }

    ESP_LOGI(TAG, "agent started: %s %s on %s, server %s", s_cfg.app_name, s_cfg.version, s_cfg.hw,
             server_url()[0] ? server_url() : "to be found over mDNS");
    s_started = true;
    notify_config(); /* the saved configuration applies from boot, even offline */
    return ESP_OK;
}

esp_err_t otter_start(const otter_config_t *config)
{
    ESP_RETURN_ON_ERROR(init(config), TAG, "init failed");
    return xTaskCreate(otter_task, "otter", 8192, NULL, 5, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

/* A new firmware that never reaches the server is rolled back after this many wake-ups. */
#define ONE_SHOT_VERIFY_BOOTS 3

/* Deep sleep: without a long uptime, a new firmware's validation counts wake-ups in NVS. */
static void one_shot_verify(bool reached)
{
    nvs_handle_t nvs;
    if (nvs_open("otter", NVS_READWRITE, &nvs) != ESP_OK) {
        return;
    }
    if (reached) {
        nvs_erase_key(nvs, "pv_boots");
    } else {
        uint8_t boots = 0;
        nvs_get_u8(nvs, "pv_boots", &boots);
        nvs_set_u8(nvs, "pv_boots", ++boots);
        if (boots >= ONE_SHOT_VERIFY_BOOTS) {
            nvs_erase_key(nvs, "pv_boots");
            nvs_commit(nvs);
            nvs_close(nvs);
            ESP_LOGE(TAG, "new firmware couldn't reach the server in %d wake-ups, rolling back", boots);
            esp_ota_mark_app_invalid_rollback_and_reboot();
        }
    }
    nvs_commit(nvs);
    nvs_close(nvs);
}

static esp_err_t checkin_rounds(void);

esp_err_t otter_checkin_once(const otter_config_t *config, uint32_t next_checkin_s)
{
    s_one_shot = true;
    s_next_checkin_s = next_checkin_s;
    ESP_RETURN_ON_ERROR(init(config), TAG, "init failed");

    /* Awake for a few seconds only: modem sleep saves nothing worth it, and on a weak link the
     * server's answer, buffered by the access point, got lost more often than not. */
    wifi_ps_type_t saved_ps = WIFI_PS_NONE;
    bool ps_changed = esp_wifi_get_ps(&saved_ps) == ESP_OK && saved_ps != WIFI_PS_NONE &&
                      esp_wifi_set_ps(WIFI_PS_NONE) == ESP_OK;
    esp_err_t result = checkin_rounds();
    if (ps_changed) {
        esp_wifi_set_ps(saved_ps);
    }
    return result;
}

static esp_err_t checkin_rounds(void)
{
    /* A few rounds: to confirm a configuration, fetch commands queued behind others, or check
     * in again after a failed update. */
    for (int round = 0; round < 5; round++) {
        update_order_t order;
        bool reached = checkin(&order);
        if (reached) {
            report_crash();
        }
        if (s_pending_verify) {
            one_shot_verify(reached);
            if (reached && !s_cfg.manual_mark_valid) {
                otter_mark_valid();
            }
        }
        if (s_reboot_requested) {
            ESP_LOGW(TAG, "rebooting, as asked from Otter");
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }
        if (order.deployment_id) {
            apply_update(&order); /* restarts into the new firmware, returns on failure */
            continue;
        }
        if (!reached && round == 0) {
            /* A link that just woke up often drops the first packets: one more try. */
            ESP_LOGW(TAG, "check-in failed, trying again in 2 s");
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        if (!reached) {
            return ESP_FAIL;
        }
        if (!s_checkin_again) {
            break;
        }
        s_checkin_again = false;
    }
    close_kept(&s_report_conn);
    return ESP_OK;
}
