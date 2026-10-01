#!/bin/sh
# Upload a built firmware image to Otter.
#   tools/push.sh <firmware.bin> <app> <hw> <version> [notes]
# Server taken from $OTTER_SERVER (default http://localhost:8000), API token from
# $OTTER_TOKEN (python -m otter.cli create-token push --user <you>), and publishes on the release
# channel $OTTER_CHANNEL if set. With $OTTER_SIGNING_KEY (path to a private key PEM), the image is
# signed here and the signature uploaded with it (see "Signed firmware" in the README).
# The ELF file next to the image (firmware.elf), if any, goes along to decode crash reports,
# and so does factory.bin, to install the firmware on new boards from the dashboard.
set -eu
[ $# -ge 4 ] || { sed -n 2,10p "$0"; exit 1; }
elf="${1%.bin}.elf"
factory="$(dirname "$1")/factory.bin"
signature=
if [ -n "${OTTER_SIGNING_KEY:-}" ]; then
  signature=$(openssl dgst -sha256 -sign "$OTTER_SIGNING_KEY" "$1" | base64 | tr -d '\n')
fi
curl -fsS ${OTTER_TOKEN:+-H "Authorization: Bearer $OTTER_TOKEN"} \
  -F "file=@$1" -F "app=$2" -F "hw=$3" -F "version=$4" -F "notes=${5:-}" ${OTTER_CHANNEL:+-F "channel=$OTTER_CHANNEL"} \
  ${signature:+-F "signature=$signature"} \
  $( [ -f "$elf" ] && printf '%s' "-F elf=@$elf" ) \
  $( [ -f "$factory" ] && printf '%s' "-F factory=@$factory" ) \
  "${OTTER_SERVER:-http://localhost:8000}/api/firmwares"
echo
