idf_component_register(SRCS "main.c" INCLUDE_DIRS "." REQUIRES otter esp_wifi esp_netif nvs_flash esp_event esp_driver_gpio)

# CA certificate the device trusts for an https:// server, e.g. Caddy's local CA: set
# OTTER_CA_CERT at build time to the PEM itself (as CI secrets do) or to its path (relative
# paths start from the project). Without it, the ESP-IDF certificate bundle is used: public
# CAs such as Let's Encrypt.
set(ca_header ${CMAKE_CURRENT_BINARY_DIR}/otter_ca.h)
if(DEFINED ENV{OTTER_CA_CERT} AND NOT "$ENV{OTTER_CA_CERT}" STREQUAL "")
    set(pem "$ENV{OTTER_CA_CERT}")
    if(NOT pem MATCHES "-----BEGIN")
        get_filename_component(ca_file "${pem}" ABSOLUTE BASE_DIR ${CMAKE_SOURCE_DIR})
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${ca_file})
        file(READ ${ca_file} pem)
    endif()
    string(REPLACE "\r" "" pem "${pem}")
    string(STRIP "${pem}" pem)
    string(REPLACE "\n" "\\n\"\n    \"" pem "${pem}")
    file(WRITE ${ca_header} "#define OTTER_HAS_CA 1\nstatic const char OTTER_CA_PEM[] =\n    \"${pem}\\n\";\n")
else()
    file(WRITE ${ca_header} "/* No OTTER_CA_CERT at build time. */\n")
endif()
target_include_directories(${COMPONENT_LIB} PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
