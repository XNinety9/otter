set(priv_requires app_update esp_app_format esp_http_client esp_netif esp_timer esp_wifi mbedtls nvs_flash)
# ESP-IDF 6 moved cJSON to the component registry (see idf_component.yml).
if(IDF_VERSION_MAJOR LESS 6)
    list(APPEND priv_requires json)
endif()

idf_component_register(
    SRCS "otter_agent.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES ${priv_requires}
)
