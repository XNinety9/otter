"""PlatformIO post-build script: writes factory.bin next to firmware.bin, the whole flash
(bootloader, partition table, OTA data, app) in one image to write at offset 0. Otter's web
installer flashes new boards with it, and tools/push.sh uploads it with the app image.

    extra_scripts = post:../../scripts/factory_image.py

ESP8266 images are already whole: firmware.bin is copied.
"""

import shutil
import subprocess

Import("env")  # noqa: F821 (PlatformIO)


def factory_image(source, target, env):
    build = env.subst("$BUILD_DIR")
    app = env.subst("$BUILD_DIR/${PROGNAME}.bin")
    out = f"{build}/factory.bin"
    if env.get("PIOPLATFORM") == "espressif8266":
        shutil.copyfile(app, out)
        return
    # Offsets come as "0x8000" strings or as ints, depending on the framework.
    images = [(int(env.subst(str(offset)), 0), env.subst(path)) for offset, path in env.get("FLASH_EXTRA_IMAGES", [])]
    images.append((int(env.subst("$ESP32_APP_OFFSET"), 0), app))
    subprocess.run(
        [
            env.subst("$PYTHONEXE"), env.subst("$UPLOADER"),
            "--chip", env.BoardConfig().get("build.mcu"),
            # The images already carry the flash settings the build chose: keep them.
            "merge_bin", "-o", out, "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep",
            *[part for offset, path in sorted(images) for part in (hex(offset), path)],
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"Factory image: {out}")


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", factory_image)  # noqa: F821
