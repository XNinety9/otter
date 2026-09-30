"""Firmware images on disk, content-addressed by SHA-256, and their ELF files (for crash reports)."""

import gzip
import hashlib
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


def delete_firmware_file(sha256: str) -> None:
    firmware_path(sha256).unlink(missing_ok=True)


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
