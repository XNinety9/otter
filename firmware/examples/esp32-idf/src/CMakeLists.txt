idf_component_register(SRCS "main.c" INCLUDE_DIRS "." REQUIRES otter esp_wifi esp_netif nvs_flash esp_event esp_driver_gpio)

# Writes header, defining has_macro and the C string pem_name, from the PEM in the environment
# variable env_var: the PEM itself (as CI secrets do) or its path (relative paths start from
# the project). Without it, the header defines nothing.
function(otter_embed_pem env_var header has_macro pem_name)
    if(NOT DEFINED ENV{${env_var}} OR "$ENV{${env_var}}" STREQUAL "")
        file(WRITE ${header} "/* No ${env_var} at build time. */\n")
        return()
    endif()
    set(pem "$ENV{${env_var}}")
    if(NOT pem MATCHES "-----BEGIN")
        get_filename_component(pem_file "${pem}" ABSOLUTE BASE_DIR ${CMAKE_SOURCE_DIR})
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${pem_file})
        file(READ ${pem_file} pem)
    endif()
    string(REPLACE "\r" "" pem "${pem}")
    string(STRIP "${pem}" pem)
    string(REPLACE "\n" "\\n\"\n    \"" pem "${pem}")
    file(WRITE ${header} "#define ${has_macro} 1\nstatic const char ${pem_name}[] =\n    \"${pem}\\n\";\n")
endfunction()

# CA certificate the device trusts for an https:// server, e.g. Caddy's local CA. Without it,
# the ESP-IDF certificate bundle is used: public CAs such as Let's Encrypt.
otter_embed_pem(OTTER_CA_CERT ${CMAKE_CURRENT_BINARY_DIR}/otter_ca.h OTTER_HAS_CA OTTER_CA_PEM)
# Public key updates must be signed with (tools/release.sh derives it from OTTER_SIGNING_KEY).
# Without it, the device accepts unsigned images.
otter_embed_pem(OTTER_SIGNING_PUBKEY ${CMAKE_CURRENT_BINARY_DIR}/otter_signing.h
                OTTER_HAS_SIGNING_KEY OTTER_SIGNING_PUBKEY_PEM)
target_include_directories(${COMPONENT_LIB} PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
