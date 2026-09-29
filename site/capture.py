"""Record the dashboard demo shown on the website (site/assets/demo.*).

Needs a running Otter server fed by the simulator, an account and an API token, Chromium,
Playwright's video encoder (`playwright install ffmpeg`) and ffmpeg:

    uvx --with playwright python site/capture.py http://localhost:8766 otk_… admin 'password'

It names and tags the simulated weather stations, starts a staged rollout of the newest
weather-station firmware, records the dashboard until the rollout completes, then encodes
MP4 and WebM files and a poster image with ffmpeg.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE, TOKEN, USER, PASSWORD = sys.argv[1:5]
OUT = pathlib.Path(__file__).parent / "assets"
NAMES = ["Garden", "Greenhouse", "Balcony", "Attic", "Garage", "Rooftop"]


def api(method, path, body=None):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def prepare():
    stations = [d for d in api("GET", "/api/devices") if d["app"] == "weather-station"]
    for device, name in zip(sorted(stations, key=lambda d: d["mac"]), NAMES):
        tags = ["outdoor"] if name in ("Garden", "Balcony", "Rooftop") else ["indoor"]
        api("PATCH", f"/api/devices/{device['id']}", {"name": f"{name} station", "tags": tags})
    firmware = max(
        (f for f in api("GET", "/api/firmwares") if f["app"] == "weather-station"), key=lambda f: f["id"]
    )
    return firmware


def main():
    firmware = prepare()
    video_dir = pathlib.Path(tempfile.mkdtemp())
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("chromium") or None)
        context = browser.new_context(
            viewport={"width": 1280, "height": 860},
            color_scheme="dark",
            record_video_dir=str(video_dir),
            record_video_size={"width": 1280, "height": 860},
        )
        context.request.post(BASE + "/api/auth/login", data={"username": USER, "password": PASSWORD})
        page = context.new_page()
        page.goto(BASE + "/?q=station")
        page.wait_for_selector("#devices tbody tr")
        page.wait_for_timeout(1500)
        api("POST", "/api/rollouts", {"firmware_id": firmware["id"], "stages": [34, 100], "soak_s": 0})
        page.wait_for_selector(".rollout .badge.completed", timeout=120_000)
        page.wait_for_timeout(2500)
        context.close()
        browser.close()
    raw = next(video_dir.glob("*.webm"))
    # Drop the first half second (blank page load), encode for the web.
    common = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(raw), "-an", "-vf", "fps=24"]
    subprocess.run([*common, "-c:v", "libx264", "-crf", "28", "-preset", "slow", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(OUT / "demo.mp4")], check=True)
    subprocess.run([*common, "-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0", str(OUT / "demo.webm")], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-2", "-i", str(raw), "-frames:v", "1",
                    "-quality", "80", str(OUT / "demo-poster.webp")], check=True)
    for f in ("demo.mp4", "demo.webm", "demo-poster.webp"):
        print(f"{f}: {(OUT / f).stat().st_size // 1024} KB")


if __name__ == "__main__":
    start = time.time()
    main()
    print(f"done in {time.time() - start:.0f} s")
