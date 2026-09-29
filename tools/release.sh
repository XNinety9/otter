#!/usr/bin/env bash
# Build every environment of a firmware app at a given version, and upload the images to Otter.
#   tools/release.sh <app> <version>
#
# Apps, their PlatformIO project and the hardware of each environment: firmware/apps.json.
# Builds are clean, and each image is checked to embed <version>.
#
# Build settings, baked into the firmware: WIFI_SSID, WIFI_PASS, OTTER_SERVER, OTTER_FLEET_KEY,
# and OTTER_CA_CERT: a private CA certificate (PEM, or the path to one) that devices and the
# upload trust, e.g. Caddy's local CA for an https:// server.
# OTTER_SIGNING_KEY (private key PEM, or the path to one): images are signed with it, and built
# to accept only images signed with it (see "Signed firmware" in the README).
# Upload happens when OTTER_TOKEN is set: to OTTER_PUSH_URL (default OTTER_SERVER), on the
# channel OTTER_CHANNEL if set.
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -eq 2 ] || { sed -n 2,14p "$0"; exit 1; }
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
fi
if [ -n "${OTTER_CA_CERT:-}" ]; then
  if [[ $OTTER_CA_CERT == *"-----BEGIN"* ]]; then
    export CURL_CA_BUNDLE=$(mktemp); printf '%s\n' "$OTTER_CA_CERT" > "$CURL_CA_BUNDLE"
  else
    export OTTER_CA_CERT=$(realpath "$OTTER_CA_CERT") CURL_CA_BUNDLE=$(realpath "$OTTER_CA_CERT")
  fi
fi

if [ -n "${OTTER_SIGNING_KEY:-}" ]; then
  if [[ $OTTER_SIGNING_KEY == *"-----BEGIN"* ]]; then
    key_file=$(mktemp); chmod 600 "$key_file"; trap 'rm -f "$key_file"' EXIT
    printf '%s\n' "$OTTER_SIGNING_KEY" > "$key_file"
    export OTTER_SIGNING_KEY=$key_file
  else
    export OTTER_SIGNING_KEY=$(realpath "$OTTER_SIGNING_KEY")
  fi
  # The firmware embeds the matching public key: devices then refuse anything else.
  export OTTER_SIGNING_PUBKEY=$(openssl pkey -in "$OTTER_SIGNING_KEY" -pubout)
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
