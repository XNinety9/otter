#!/bin/sh
# Upload a built firmware image to Otter.
#   tools/push.sh <firmware.bin> <app> <hw> <version> [notes]
# Server taken from $OTTER_SERVER (default http://localhost:8000), API token from
# $OTTER_TOKEN (python -m otter.cli create-token push --user <you>), and publishes on the release
# channel $OTTER_CHANNEL if set.
set -eu
[ $# -ge 4 ] || { sed -n 2,6p "$0"; exit 1; }
curl -fsS ${OTTER_TOKEN:+-H "Authorization: Bearer $OTTER_TOKEN"} \
  -F "file=@$1" -F "app=$2" -F "hw=$3" -F "version=$4" -F "notes=${5:-}" ${OTTER_CHANNEL:+-F "channel=$OTTER_CHANNEL"} \
  "${OTTER_SERVER:-http://localhost:8000}/api/firmwares"
echo
