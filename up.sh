#!/usr/bin/env bash
# Deploy frigate-dejavu: ensure the shared clips dir exists on the frigate
# config mount (so the frigate container needs no compose change), then build
# and start the appliance.
set -Eeuo pipefail
cd "$(dirname "$0")"

mkdir -p ../frigate/config/dejavu-clips data

docker compose up -d --build "$@"
