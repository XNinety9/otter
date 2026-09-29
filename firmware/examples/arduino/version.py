"""Defines APP_VERSION from OTTER_VERSION, or custom_otter_version in platformio.ini."""

import os

Import("env")  # noqa: F821 (provided by PlatformIO)

version = os.environ.get("OTTER_VERSION") or env.GetProjectOption("custom_otter_version")  # noqa: F821
env.Append(CPPDEFINES=[("APP_VERSION", env.StringifyMacro(version))])  # noqa: F821
