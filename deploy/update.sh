#!/usr/bin/env bash
# Holt neue Images und startet nur Container neu, deren Image sich geändert hat.
# Wird vom systemd-Timer "homelab-update" aufgerufen, geht aber auch von Hand.
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/stack}"
cd "$STACK_DIR"

docker compose pull --quiet
docker compose up -d
docker image prune -f >/dev/null
