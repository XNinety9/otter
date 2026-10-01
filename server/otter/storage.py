"""Firmware images on disk, content-addressed by SHA-256, and their ELF files (for crash reports)."""

import gzip
import hashlib
import zlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import config

# First byte of every ESP32 / ESP8266 application image.
ESP_IMAGE_MAGIC = 0xE9
# esp_app_desc_t, at the start of an ESP32 image's first segment (not in ESP8266 images).
APP_DESC_MAGIC = b"\x32\x54\xcd\xab"
APP_DESC_ELF_SHA256 = 0x90  # offset of app_elf_sha256[32] in esp_app_desc_t
MAX_FACTORY_SIZE = 32 << 20  # the biggest ESP flash


def firmware_path(sha256: str) -> Path:
    return config.FIRMWARE_DIR / f"{sha256}.bin"


def store_firmware(src: BinaryIO) -> tuple[str, int]:
    """Copy an uploaded image into the store. Returns (sha256, size)."""
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    first = True
    with tempfile.NamedTemporaryFile(dir=config.FIRMWARE_DIR, delete=False) as tmp:
        try:
            while chunk := src.read(64 * 1024):
                if first and chunk[0] != ESP_IMAGE_MAGIC:
                    raise ValueError("not an ESP application image (bad magic byte)")
                first = False
                digest.update(chunk)
                size += len(chunk)
                tmp.write(chunk)
            if size == 0:
                raise ValueError("empty file")
        except Exception:
            os.unlink(tmp.name)
            raise
    sha = digest.hexdigest()
    os.replace(tmp.name, firmware_path(sha))
    return sha, size


def factory_path(sha256: str) -> Path:
    return config.FIRMWARE_DIR / f"{sha256}.factory.bin"


def store_factory(src: BinaryIO) -> str:
    """Keeps a factory image: the whole flash, to write at offset 0 on a new board. Returns
    its sha256. It starts with the bootloader, or with padding where the bootloader sits at
    0x1000 (ESP32, ESP32-S2): no magic byte to check."""
    config.FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=config.FIRMWARE_DIR, delete=False) as tmp:
        try:
            while chunk := src.read(64 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if size > MAX_FACTORY_SIZE:
                    raise ValueError("factory image bigger than 32 MB")
                tmp.write(chunk)
            if size == 0:
                raise ValueError("empty factory image")
        except Exception:
            os.unlink(tmp.name)
            raise
    sha = digest.hexdigest()
    os.replace(tmp.name, factory_path(sha))
    return sha


def delete_firmware_file(sha256: str) -> None:
    firmware_path(sha256).unlink(missing_ok=True)
    compressed_path(sha256).unlink(missing_ok=True)
    delete_deltas(sha256)


def image_elf_sha256(image: Path) -> str | None:
    """SHA-256 of the ELF file an ESP32 image was built from, as its app description says."""
    with image.open("rb") as f:
        head = f.read(1024)
    start = head.find(APP_DESC_MAGIC)
    if start < 0 or len(head) < start + APP_DESC_ELF_SHA256 + 32:
        return None
    sha = head[start + APP_DESC_ELF_SHA256 : start + APP_DESC_ELF_SHA256 + 32]
    return sha.hex() if any(sha) else None


def elf_path(elf_sha256: str) -> Path:
    return config.FIRMWARE_DIR / f"{elf_sha256}.elf.gz"


def store_elf(src: BinaryIO, expected_sha256: str) -> None:
    """Keeps an ELF file, compressed, if it is the one the image was built from."""
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=config.FIRMWARE_DIR, delete=False) as tmp:
        try:
            with gzip.GzipFile(fileobj=tmp, mode="wb", compresslevel=6) as out:
                first = True
                while chunk := src.read(256 * 1024):
                    if first and not chunk.startswith(b"\x7fELF"):
                        raise ValueError("not an ELF file")
                    first = False
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("this ELF file isn't the one the image was built from")
        except Exception:
            os.unlink(tmp.name)
            raise
    os.replace(tmp.name, elf_path(expected_sha256))


def open_elf(elf_sha256: str):
    """The decompressed ELF file, as a seekable file object."""
    buffer = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with gzip.open(elf_path(elf_sha256), "rb") as src:
        shutil.copyfileobj(src, buffer)
    buffer.seek(0)
    return buffer


def delete_elf(elf_sha256: str) -> None:
    elf_path(elf_sha256).unlink(missing_ok=True)


# A compressed copy is kept when it saves at least this much (#25): images already compressed
# or encrypted don't shrink, and then devices download the plain image.
MIN_COMPRESSION_GAIN = 0.05


def compressed_path(sha256: str) -> Path:
    return config.FIRMWARE_DIR / f"{sha256}.zlib"


def compress_firmware(sha256: str, size: int) -> int | None:
    """Writes a zlib copy of the image; returns its size, or None when not worth it."""
    data = firmware_path(sha256).read_bytes()
    packed = zlib.compress(data, 9)
    if len(packed) > size * (1 - MIN_COMPRESSION_GAIN):
        return None
    with tempfile.NamedTemporaryFile(dir=config.FIRMWARE_DIR, delete=False) as tmp:
        tmp.write(packed)
    os.replace(tmp.name, compressed_path(sha256))
    return len(packed)


# --- Delta updates (#26) -----------------------------------------------------------

# Byte of the ESP32 image header saying whether a SHA-256 of the image follows it.
HASH_APPENDED_OFFSET = 23


def image_hash(sha256: str) -> str | None:
    """The hash an ESP32 image carries at its end, which is what a device computes for the image
    it runs (esp_partition_get_sha256): it tells whether the device runs exactly this image."""
    path = firmware_path(sha256)
    with path.open("rb") as f:
        header = f.read(24)
        if len(header) < 24 or header[0] != ESP_IMAGE_MAGIC or header[HASH_APPENDED_OFFSET] != 1:
            return None
        f.seek(-32, os.SEEK_END)
        return f.read(32).hex()


def delta_path(base_sha256: str, sha256: str) -> Path:
    return config.FIRMWARE_DIR / "deltas" / f"{base_sha256}-{sha256}.patch"


def delta_size(base_sha256: str, sha256: str) -> int:
    """Size of the patch turning the base image into this one, made (and kept) if needed."""
    import detools  # only needed here

    path = delta_path(base_sha256, sha256)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            firmware_path(base_sha256).open("rb") as base,
            firmware_path(sha256).open("rb") as new,
            tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp,
        ):
            detools.create_patch(base, new, tmp, compression="heatshrink")
        os.replace(tmp.name, path)
    return path.stat().st_size


def delete_deltas(sha256: str) -> None:
    for path in (config.FIRMWARE_DIR / "deltas").glob("*.patch"):
        if sha256 in path.stem.split("-"):
            path.unlink(missing_ok=True)
