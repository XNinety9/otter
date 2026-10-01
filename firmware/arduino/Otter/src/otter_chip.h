#pragma once

/*
 * The exact chip an ESP32 runs on, named as esptool names it ("ESP32-D0WD-V3",
 * "ESP32-C6FH4 (QFN32)"…), and its revision ("0.1"): read from esp_chip_info() and the
 * eFuses. Otter shows them on the dashboard.
 *
 * The ESP-IDF component and the Arduino library each have a copy of this file: keep them
 * identical (CI checks).
 */

#include <stdint.h>
#include <stdio.h>

#include "esp_chip_info.h"
#include "esp_efuse.h"
#include "esp_efuse_table.h"
#include "sdkconfig.h"

static inline uint32_t otter_efuse_field(const esp_efuse_desc_t *field[])
{
    uint32_t value = 0;
    esp_efuse_read_field_blob(field, &value, field[0]->bit_count);
    return value;
}

static inline const char *otter_chip_model(void)
{
    esp_chip_info_t info;
    esp_chip_info(&info);
    uint32_t pkg = esp_efuse_get_pkg_ver();
    (void)pkg;
#if CONFIG_IDF_TARGET_ESP32
    int rev3 = info.revision / 100 == 3;
    switch (pkg) {
        case 0: return info.cores == 1 ? "ESP32-S0WDQ6" : rev3 ? "ESP32-D0WDQ6-V3" : "ESP32-D0WDQ6";
        case 1: return info.cores == 1 ? "ESP32-S0WD" : rev3 ? "ESP32-D0WD-V3" : "ESP32-D0WD";
        case 2: return "ESP32-D2WD";
        case 4: return "ESP32-U4WDH";
        case 5: return rev3 ? "ESP32-PICO-V3" : "ESP32-PICO-D4";
        case 6: return "ESP32-PICO-V3-02";
        case 7: return "ESP32-D0WDR2-V3";
        default: return "ESP32";
    }
#elif CONFIG_IDF_TARGET_ESP32S2
    switch (otter_efuse_field(ESP_EFUSE_FLASH_VERSION) + otter_efuse_field(ESP_EFUSE_PSRAM_VERSION) * 100) {
        case 1: return "ESP32-S2FH2";
        case 2: return "ESP32-S2FH4";
        case 100: return "ESP32-S2R2";
        case 102: return "ESP32-S2FNR2";
        default: return "ESP32-S2";
    }
#elif CONFIG_IDF_TARGET_ESP32S3
    return pkg == 1 ? "ESP32-S3-PICO-1 (LGA56)" : "ESP32-S3 (QFN56)";
#elif CONFIG_IDF_TARGET_ESP32C3
    switch (pkg) {
        case 1: return "ESP8685 (QFN28)";
        case 2: return "ESP32-C3 AZ (QFN32)";
        case 3: return "ESP8686 (QFN24)";
        default: return "ESP32-C3 (QFN32)";
    }
#elif CONFIG_IDF_TARGET_ESP32C6
    if (pkg == 0) {
        return "ESP32-C6 (QFN40)";
    }
    switch (otter_efuse_field(ESP_EFUSE_FLASH_CAP)) {  // both have package 1
        case 1: return "ESP32-C6FH4 (QFN32)";
        case 2: return "ESP32-C6FH8 (QFN32)";
        default: return "ESP32-C6 (QFN32)";
    }
#elif CONFIG_IDF_TARGET_ESP32C2
    return "ESP32-C2";
#elif CONFIG_IDF_TARGET_ESP32C5
    return "ESP32-C5";
#elif CONFIG_IDF_TARGET_ESP32C61
    return "ESP32-C61";
#elif CONFIG_IDF_TARGET_ESP32H2
    return "ESP32-H2";
#elif CONFIG_IDF_TARGET_ESP32P4
    return "ESP32-P4";
#else
    return CONFIG_IDF_TARGET;
#endif
}

/* "major.minor", e.g. "3.1". */
static inline void otter_chip_revision(char *buf, size_t size)
{
    esp_chip_info_t info;
    esp_chip_info(&info);
    snprintf(buf, size, "%d.%d", info.revision / 100, info.revision % 100);
}
