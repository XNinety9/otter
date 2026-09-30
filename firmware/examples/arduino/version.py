"""Build settings from the environment: APP_VERSION from OTTER_VERSION (or custom_otter_version
in platformio.ini); OTTER_SIGNING_PUBKEY_PEM from OTTER_SIGNING_PUBKEY (a PEM public key, or its
path) when updates must be signed; OTTER_CA_PEM from OTTER_CA_CERT (PEM or path) for an https://
server with a private CA."""

import os

Import("env")  # noqa: F821 (provided by PlatformIO)

version = os.environ.get("OTTER_VERSION") or env.GetProjectOption("custom_otter_version")  # noqa: F821
env.Append(CPPDEFINES=[("APP_VERSION", env.StringifyMacro(version))])  # noqa: F821



def pem_define(variable: str, define: str) -> None:
    value = os.environ.get(variable, "")
    if not value:
        return
    if "-----BEGIN" not in value:
        with open(os.path.join(env.subst("$PROJECT_DIR"), value)) as f:  # noqa: F821
            value = f.read()
    pem = value.strip().replace("\n", "\\n") + "\\n"
    env.Append(CPPDEFINES=[(define, env.StringifyMacro(pem))])  # noqa: F821


pem_define("OTTER_SIGNING_PUBKEY", "OTTER_SIGNING_PUBKEY_PEM")
pem_define("OTTER_CA_CERT", "OTTER_CA_PEM")
