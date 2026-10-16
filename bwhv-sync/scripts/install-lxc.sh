#!/usr/bin/env bash
# Einrichtung im Proxmox-LXC (Debian 12/13). Als root im Projektordner ausführen:
#   bash scripts/install-lxc.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo ">> Docker wird installiert …"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates
  curl -fsSL https://get.docker.com | sh
fi

mkdir -p data

# Alte Dateien (Pi-Layout) in den Datenordner übernehmen
for f in client_secret.json token.json; do
  if [ -s "$f" ] && [ ! -e "data/$f" ]; then
    mv "$f" "data/$f"
    echo ">> $f nach data/ verschoben"
  fi
done

if [ ! -s data/client_secret.json ]; then
  echo "!! data/client_secret.json fehlt – App startet trotzdem und zeigt einen Hinweis."
fi

docker compose up -d --build

IP=$(hostname -I | awk '{print $1}')
echo
echo ">> Fertig. Aufrufen unter:  http://${IP}:${BWHV_PORT:-5000}"
