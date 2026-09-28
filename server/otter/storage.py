"""Firmware images on disk, content-addressed by SHA-256."""

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import config

# First byte of every ESP32 / ESP8266 application image.
ESP_IMAGE_MAGIC = 0xE9


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
