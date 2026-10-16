#!/usr/bin/env bash
# Einmalige Einrichtung auf docker1 (CT 301).
# Als root IM STACK-ORDNER ausführen – dort, wo compose.yaml und die Ordner
# bwhv-sync/, untis-sync/, obsidian-livesync/ liegen:
#
#   nano setup-docker1.sh      # Inhalt dieser Datei einfügen, speichern
#   bash setup-docker1.sh
#
# Was passiert:
#  1. Login bei ghcr.io (GitHub-Token "classic" mit NUR read:packages)
#  2. compose.yaml wird gesichert und durch die Image-Version ersetzt
#  3. homelab-update + systemd-Timer (alle 15 Min.) werden installiert
#  4. Images werden das erste Mal gezogen und gestartet
#
# compose.yaml und update.sh hier drin sind Kopien von deploy/compose.yaml und
# deploy/update.sh – bei Änderungen beide Stellen anpassen.
set -euo pipefail

STACK_DIR="$(pwd)"
if [ ! -f compose.yaml ]; then
  echo "!! Hier liegt keine compose.yaml. Bitte im Stack-Ordner ausführen." >&2
  exit 1
fi

read -rp  "GitHub-Benutzername [LinoKing09]: " GH_USER
GH_USER="${GH_USER:-LinoKing09}"
read -rsp "GitHub-Token (classic, nur read:packages): " GH_TOKEN; echo

echo ">> Login bei ghcr.io …"
echo "$GH_TOKEN" | docker login ghcr.io -u "$GH_USER" --password-stdin
unset GH_TOKEN

echo ">> compose.yaml sichern und ersetzen …"
cp compose.yaml "compose.yaml.bak-$(date +%Y%m%d-%H%M%S)"
cat > compose.yaml <<'COMPOSE_EOF'
# compose.yaml für docker1 (CT 301).
# Liegt im Stack-Ordner auf docker1; die Datenordner bleiben wie bisher daneben.
# Eigene Apps kommen als fertige Images aus GHCR (gebaut von GitHub Actions),
# kein lokales "build:" mehr nötig.

services:
  bwhv-sync:
    image: ghcr.io/linoking09/bwhv-sync:latest
    container_name: bwhv-sync
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./bwhv-sync/data:/app/data
    environment:
      - TZ=Europe/Berlin
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "3"

  untis-sync:
    image: ghcr.io/linoking09/untis-sync:latest
    container_name: untis-sync
    restart: unless-stopped
    ports:
      - "5001:5000"        # 5000 ist schon von bwhv-sync belegt
    env_file: ./untis-sync/.env
    volumes:
      - ./untis-sync/data:/data
      - ./untis-sync/client_secret.json:/app/client_secret.json:ro
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "3"

  obsidian-livesync:
    container_name: obsidian-livesync
    image: couchdb:3
    restart: unless-stopped
    env_file:
      - ./obsidian-livesync/.env
    volumes:
      - ./obsidian-livesync/data:/opt/couchdb/data
      - ./obsidian-livesync/etc:/opt/couchdb/etc/local.d
    ports:
      - "5984:5984"
    healthcheck:
      test: ["CMD-SHELL", "curl -fs -u \"$${COUCHDB_USER}:$${COUCHDB_PASSWORD}\" http://localhost:5984/_up | grep -q ok"]
      interval: 30s
      timeout: 10s
      retries: 3
COMPOSE_EOF

echo ">> homelab-update installieren …"
cat > /usr/local/bin/homelab-update <<'UPDATE_EOF'
#!/usr/bin/env bash
# Holt neue Images und startet nur Container neu, deren Image sich geändert hat.
# Wird vom systemd-Timer "homelab-update" aufgerufen, geht aber auch von Hand.
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/stack}"
cd "$STACK_DIR"

docker compose pull --quiet
docker compose up -d
docker image prune -f >/dev/null
UPDATE_EOF
chmod +x /usr/local/bin/homelab-update

cat > /etc/systemd/system/homelab-update.service <<UNIT
[Unit]
Description=Homelab: neue Container-Images holen und neu starten
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=HOME=/root
Environment=STACK_DIR=$STACK_DIR
ExecStart=/usr/local/bin/homelab-update
UNIT

cat > /etc/systemd/system/homelab-update.timer <<'UNIT'
[Unit]
Description=Homelab-Update alle 15 Minuten

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now homelab-update.timer

echo ">> Erster Start mit den GitHub-Images …"
STACK_DIR="$STACK_DIR" /usr/local/bin/homelab-update
docker compose ps

echo
echo ">> Fertig. Updates kommen ab jetzt automatisch alle 15 Minuten."
echo "   Sofort aktualisieren: homelab-update"
echo "   Log:                  journalctl -u homelab-update -n 50"
echo "   Zurück zum alten Stand: compose.yaml.bak-* zurückkopieren, docker compose up -d --build"
