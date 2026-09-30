cmake_minimum_required(VERSION 3.16)

# [ADAPT] The firmware version reported to Otter: bump it before building an update (Otter
# compares versions like 1.10.0 > 1.9.2), or set OTTER_VERSION in the environment, as
# tools/release.sh and CI do. It is read when the build is configured: after changing it, clean
# the build (rm -rf .pio/build/<env>) or the old version stays in the image.
if(DEFINED ENV{OTTER_VERSION} AND NOT "$ENV{OTTER_VERSION}" STREQUAL "")
    set(PROJECT_VER "$ENV{OTTER_VERSION}")
else()
    set(PROJECT_VER "1.5.0")
endif()

# The Otter agent component. In your own project, point this at a copy of firmware/components
# (or a git submodule of this repository).
list(APPEND EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../../components)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# [ADAPT] The ESP-IDF project name: only a build detail, Otter uses OTTER_APP_NAME in main.c.
project(otter-demo)
