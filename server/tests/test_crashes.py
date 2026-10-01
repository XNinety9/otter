"""Crash reports (#24): matched with their firmware through the ELF hash, decoded from its ELF."""

import hashlib
import shutil
import subprocess

import pytest
from elftools.elf.elffile import ELFFile

from otter.storage import APP_DESC_ELF_SHA256, APP_DESC_MAGIC
from conftest import fake_image
from test_channels import ids

MAC = "aa:bb:cc:00:00:01"
SOURCE = """
int counter;
__attribute__((noinline)) void crash_here(void) { counter = *(volatile int *)0; }
__attribute__((noinline)) void caller(void) { crash_here(); counter++; }
int main(void) { caller(); return 0; }
"""


@pytest.fixture(scope="module")
def elf(tmp_path_factory):
    """A small ELF with debug info, built for the machine running the tests: the decoder
    doesn't depend on the architecture."""
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    tmp = tmp_path_factory.mktemp("elf")
    (tmp / "crash.c").write_text(SOURCE)
    subprocess.run(["cc", "-g", "-O0", "-o", tmp / "crash.elf", tmp / "crash.c"], check=True)
    data = (tmp / "crash.elf").read_bytes()
    with open(tmp / "crash.elf", "rb") as f:
        symbols = {s.name: s["st_value"] for s in ELFFile(f).get_section_by_name(".symtab").iter_symbols()}
    return {"data": data, "sha": hashlib.sha256(data).hexdigest(), "symbols": symbols}


def image(elf_sha: str, version: str) -> bytes:
    """An ESP32-like image whose app description names the ELF."""
    desc = APP_DESC_MAGIC + bytes(APP_DESC_ELF_SHA256 - 4) + bytes.fromhex(elf_sha)
    return b"\xe9" + bytes(31) + desc + version.encode() + bytes(256)


def upload(client, elf, version="1.1.0", with_elf=True):
    files = {"file": ("fw.bin", image(elf["sha"], version))}
    if with_elf:
        files["elf"] = ("fw.elf", elf["data"])
    res = client.post("/api/firmwares", data={"app": "weather", "hw": "esp32", "version": version}, files=files)
    assert res.status_code == 201, res.text
    return res.json()


def report(client, elf, **extra):
    s = elf["symbols"]
    body = {
        "elf_sha256": elf["sha"][:8],  # devices send a prefix
        "task": "main",
        "reason": "Load access fault",
        "pc": s["crash_here"] + 4,
        "registers": {"ra": s["caller"] + 8, "sp": 0x3FC80000},
        "stack": [0x12345678, s["main"] + 12, 0],
        **extra,
    }
    res = client.post(f"/api/v1/crashes?mac={MAC}", json=body)
    assert res.status_code == 200, res.text
    return client.get(f"/api/devices/{ids(client)[MAC]}/crashes").json()[0]


def test_a_crash_reads_as_a_call_stack(client, checkin, elf):
    fw = upload(client, elf)
    assert (fw["has_elf"], fw["crash_count"]) == (True, 0)
    checkin(mac=MAC)
    crash = report(client, elf)

    assert (crash["fw_version"], crash["task"], crash["reason"], crash["decoded"]) == (
        "1.1.0", "main", "Load access fault", True,
    )
    frames = [(f["kind"], f["function"], f["file"]) for f in crash["frames"]]
    assert frames == [("pc", "crash_here", "crash.c"), ("return", "caller", "crash.c"), ("stack", "main", "crash.c")]
    assert crash["frames"][0]["line"] == 3
    assert client.get("/api/firmwares").json()[0]["crash_count"] == 1


def test_a_crash_before_its_elf_is_decoded_later(client, checkin, elf):
    checkin(mac=MAC)
    crash = report(client, elf)  # flashed over USB: not in Otter yet
    assert (crash["fw_version"], crash["decoded"]) == ("1.0.0", False)
    assert crash["frames"][0]["address"].startswith("0x")

    fw = upload(client, elf, with_elf=False)
    assert client.get(f"/api/devices/{ids(client)[MAC]}/crashes").json()[0]["fw_version"] == "1.1.0"  # adopted
    res = client.post(f"/api/firmwares/{fw['id']}/elf", files={"elf": ("fw.elf", elf["data"])})
    assert res.status_code == 200, res.text
    assert client.get(f"/api/devices/{ids(client)[MAC]}/crashes").json()[0]["decoded"] is True


def test_the_wrong_elf_is_refused(client, elf):
    files = {"file": ("fw.bin", image("ab" * 32, "1.1.0")), "elf": ("fw.elf", elf["data"])}
    res = client.post("/api/firmwares", data={"app": "weather", "hw": "esp32", "version": "1.1.0"}, files=files)
    assert (res.status_code, res.json()["detail"]) == (422, "this ELF file isn't the one the image was built from")
    assert client.get("/api/firmwares").json() == []


def test_an_esp8266_image_is_kept_without_its_elf(client, elf):
    # tools/push.sh sends the ELF whenever the build made one: ESP8266 images can't use it.
    files = {"file": ("fw.bin", fake_image(b"esp8266")), "elf": ("fw.elf", elf["data"])}
    res = client.post("/api/firmwares", data={"app": "weather", "hw": "esp8266", "version": "1.1.0"}, files=files)
    assert res.status_code == 201, res.text
    assert res.json()["has_elf"] is False


def test_xtensa_backtraces_are_used_as_is(client, checkin, elf):
    upload(client, elf)
    checkin(mac=MAC)
    s = elf["symbols"]
    crash = report(client, elf, backtrace=[s["crash_here"] + 4, s["caller"] + 8, s["main"] + 12], stack=[], registers={})
    assert [(f["kind"], f["function"]) for f in crash["frames"]] == [
        ("pc", "crash_here"), ("return", "caller"), ("return", "main"),
    ]


def test_unknown_device(client, elf):
    body = {"elf_sha256": "abcd", "pc": 1}
    assert client.post("/api/v1/crashes?mac=aa:bb:cc:00:00:99", json=body).status_code == 404
    assert client.post("/api/v1/crashes?mac=nope", json=body).status_code == 422
