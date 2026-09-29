#!/usr/bin/env bash
# Build every environment of a firmware app at a given version, and upload the images to Otter.
#   tools/release.sh <app> <version>
#
# Apps, their PlatformIO project and the hardware of each environment: firmware/apps.json.
# Builds are clean, and each image is checked to embed <version>.
#
# Build settings, baked into the firmware: WIFI_SSID, WIFI_PASS, OTTER_SERVER, OTTER_FLEET_KEY.
# Upload happens when OTTER_TOKEN is set: to OTTER_PUSH_URL (default OTTER_SERVER), on the
# channel OTTER_CHANNEL if set. OTTER_CA_CERT may hold a PEM certificate to trust (private CA).
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 2 ] || { sed -n 2,11p "$0"; exit 1; }
app=$1 version=$2
[[ $version =~ ^[0-9A-Za-z.+-]{1,32}$ ]] || { echo "invalid version: $version" >&2; exit 1; }

targets=$(python3 - "$app" <<'PY'
import json, sys
apps = json.load(open("firmware/apps.json"))
app = apps.get(sys.argv[1])
if app is None:
    sys.exit(f"unknown app {sys.argv[1]!r}; known: {', '.join(apps)}")
for env, hw in app["environments"].items():
    print(app["project"], env, hw)
PY
)

push_url=${OTTER_PUSH_URL:-${OTTER_SERVER:-}}
if [ -n "${OTTER_TOKEN:-}" ]; then
  # A firmware without the right Wi-Fi settings would cut devices off (and an ESP8266 can't roll back).
  [ -n "${WIFI_SSID:-}" ] && [ -n "${OTTER_SERVER:-}" ] \
    || { echo "refusing to upload: WIFI_SSID and OTTER_SERVER must be set for a real build" >&2; exit 1; }
  [ -n "$push_url" ] || { echo "OTTER_TOKEN is set but there is no OTTER_PUSH_URL/OTTER_SERVER" >&2; exit 1; }
  if [ -n "${OTTER_CA_CERT:-}" ]; then
    export CURL_CA_BUNDLE=$(mktemp); printf '%s\n' "$OTTER_CA_CERT" > "$CURL_CA_BUNDLE"
  fi
fi

summary=${GITHUB_STEP_SUMMARY:-/dev/null}
printf '### %s %s\n\n| Hardware | Size | SHA-256 | Uploaded |\n|---|---|---|---|\n' "$app" "$version" >> "$summary"
commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

while read -r project env hw; do
  echo "::group::$app $version for $hw ($project, env $env)"
  rm -rf "$project/.pio/build/$env"  # clean build: the version is read at configure time
  OTTER_VERSION=$version pio run -d "$project" -e "$env"
  echo "::endgroup::"
  image="$project/.pio/build/$env/firmware.bin"
  grep -aqF -- "$version" "$image" || { echo "$image doesn't embed version $version" >&2; exit 1; }
  sha=$(sha256sum "$image" | cut -d' ' -f1)
  size=$(stat -c %s "$image")
  uploaded=no
  if [ -n "${OTTER_TOKEN:-}" ]; then
    OTTER_SERVER=$push_url tools/push.sh "$image" "$app" "$hw" "$version" "release $version ($commit)" >/dev/null
    uploaded="yes${OTTER_CHANNEL:+, on $OTTER_CHANNEL}"
  fi
  printf '%-9s %-7s %8s bytes  %s  uploaded: %s\n' "$app" "$hw" "$size" "$sha" "$uploaded"
  printf '| %s | %s bytes | `%s` | %s |\n' "$hw" "$size" "$sha" "$uploaded" >> "$summary"
done <<< "$targets"
