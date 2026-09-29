cmake_minimum_required(VERSION 3.16)

# Firmware version reported to Otter: bump it before building an update,
# or set OTTER_VERSION in the environment (tools/release.sh and CI do).
if(DEFINED ENV{OTTER_VERSION} AND NOT "$ENV{OTTER_VERSION}" STREQUAL "")
    set(PROJECT_VER "$ENV{OTTER_VERSION}")
else()
    set(PROJECT_VER "1.5.0")
endif()

list(APPEND EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../../components)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(otter-demo)
