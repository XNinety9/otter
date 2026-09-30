"""Build settings from the environment: APP_VERSION from OTTER_VERSION (or custom_otter_version
in platformio.ini), and OTTER_SIGNING_PUBKEY_PEM from OTTER_SIGNING_PUBKEY (a PEM public key,
or its path) when updates must be signed."""

import os

Import("env")  # noqa: F821 (provided by PlatformIO)

version = os.environ.get("OTTER_VERSION") or env.GetProjectOption("custom_otter_version")  # noqa: F821
env.Append(CPPDEFINES=[("APP_VERSION", env.StringifyMacro(version))])  # noqa: F821

pubkey = os.environ.get("OTTER_SIGNING_PUBKEY", "")
if pubkey:
    if "-----BEGIN" not in pubkey:
        with open(os.path.join(env.subst("$PROJECT_DIR"), pubkey)) as f:  # noqa: F821
            pubkey = f.read()
    pem = pubkey.strip().replace("\n", "\\n") + "\\n"
    env.Append(CPPDEFINES=[("OTTER_SIGNING_PUBKEY_PEM", env.StringifyMacro(pem))])  # noqa: F821
