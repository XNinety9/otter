#!/bin/sh
# Upload a built firmware image to Otter.
#   tools/push.sh <firmware.bin> <app> <hw> <version> [notes]
# Server taken from $OTTER_SERVER (default http://localhost:8000), API token from
# $OTTER_TOKEN (python -m otter.cli create-token push --user <you>), and publishes on the release
# channel $OTTER_CHANNEL if set. With $OTTER_SIGNING_KEY (path to a private key PEM), the image is
# signed here and the signature uploaded with it (see "Signed firmware" in the README).
set -eu
[ $# -ge 4 ] || { sed -n 2,8p "$0"; exit 1; }
signature=
if [ -n "${OTTER_SIGNING_KEY:-}" ]; then
  signature=$(openssl dgst -sha256 -sign "$OTTER_SIGNING_KEY" "$1" | base64 | tr -d '\n')
fi
curl -fsS ${OTTER_TOKEN:+-H "Authorization: Bearer $OTTER_TOKEN"} \
  -F "file=@$1" -F "app=$2" -F "hw=$3" -F "version=$4" -F "notes=${5:-}" ${OTTER_CHANNEL:+-F "channel=$OTTER_CHANNEL"} \
  ${signature:+-F "signature=$signature"} \
  "${OTTER_SERVER:-http://localhost:8000}/api/firmwares"
echo
