#!/usr/bin/env bash
# Richtet den Kiosk auf dem Raspberry Pi ein (Raspberry Pi OS mit labwc).
# Aufruf als Benutzer lines:   bash install.sh
#
# Das Skript braucht nichts weiter neben sich: es holt kiosk.py selbst aus dem
# GitHub-Repo (nur den Ordner kiosk/pi) und richtet Autostart + Auto-Update ein.
# Zugriff auf das private Repo läuft über einen eigenen, nur lesenden
# "Deploy key", der nur für dieses eine Repo gilt.
set -euo pipefail

REPO_SSH="git@github-kiosk:LinoKing09/kincraw-homelab-apps.git"
REPO_DIR="$HOME/kincraw-homelab-apps"
KIOSK_DIR="$HOME/kiosk"
KEY="$HOME/.ssh/kiosk_deploy_key"
AUTOSTART="$HOME/.config/labwc/autostart"

echo "==> Pakete installieren"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-websocket wlopm
command -v chromium >/dev/null || command -v chromium-browser >/dev/null || sudo apt-get install -y chromium

echo "==> Zugang zu GitHub (Deploy key)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -N "" -C "kiosk-pi" -f "$KEY" -q
fi
if ! grep -q "Host github-kiosk" "$HOME/.ssh/config" 2>/dev/null; then
  cat >> "$HOME/.ssh/config" <<EOF

Host github-kiosk
    HostName github.com
    User git
    IdentityFile $KEY
    IdentitiesOnly yes
EOF
  chmod 600 "$HOME/.ssh/config"
fi
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
sort -u -o "$HOME/.ssh/known_hosts" "$HOME/.ssh/known_hosts"

github_ok() {
  local out
  out=$(ssh -o BatchMode=yes -T github-kiosk 2>&1 || true)
  grep -q "successfully authenticated" <<<"$out"
}

if ! github_ok; then
  echo
  echo "Diesen Schlüssel auf GitHub eintragen:"
  echo "  Repo kincraw-homelab-apps → Settings → Deploy keys → Add deploy key"
  echo "  Titel: kiosk-pi   |   'Allow write access' NICHT anhaken"
  echo
  cat "$KEY.pub"
  echo
  read -rp "Wenn eingetragen, Enter drücken … "
  github_ok || { echo "!! GitHub-Zugriff klappt noch nicht. Schlüssel prüfen und Skript erneut starten."; exit 1; }
fi

echo "==> Repo holen (nur kiosk/pi)"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone -q --filter=blob:none --sparse "$REPO_SSH" "$REPO_DIR"
  git -C "$REPO_DIR" sparse-checkout set kiosk/pi
else
  git -C "$REPO_DIR" pull -q --ff-only origin main
fi
chmod +x "$REPO_DIR/kiosk/pi/kiosk-run.sh"

echo "==> Alte Einzeldatei-Installation aufräumen"
mkdir -p "$KIOSK_DIR"
if [ -f "$KIOSK_DIR/kiosk.py" ]; then
  mv "$KIOSK_DIR/kiosk.py" "$KIOSK_DIR/kiosk.py.alt"
  echo "   ~/kiosk/kiosk.py → kiosk.py.alt (wird nicht mehr benutzt; config.json bleibt)"
fi

echo "==> Autostart in labwc eintragen"
mkdir -p "$(dirname "$AUTOSTART")"
touch "$AUTOSTART"
# Frühere kiosk-Einträge entfernen, andere Chromium-/Kiosk-Starts auskommentieren
sed -i -E '/kiosk-start|kiosk\/kiosk\.py|kiosk-run\.sh/d' "$AUTOSTART"
sed -i -E 's/^([^#].*(chromium|kiosk|rotate|pm2).*)$/# \1   # deaktiviert durch kiosk install/' "$AUTOSTART"
cat >> "$AUTOSTART" <<EOF
# kiosk-start: kiosk.py aus dem Git-Repo, mit Auto-Update und Neustart bei Absturz
$REPO_DIR/kiosk/pi/kiosk-run.sh &
EOF

echo "==> Bildschirmschoner/Abschalten durch das System verhindern"
sudo raspi-config nonint do_blanking 1 2>/dev/null || true

if [ ! -f "$KIOSK_DIR/config.json" ]; then
  echo "==> Erststart zum Erzeugen von config.json und Token"
  KIOSK_DIR="$KIOSK_DIR" timeout 3 python3 "$REPO_DIR/kiosk/pi/kiosk.py" >/dev/null 2>&1 || true
  pkill -f "$HOME/.chromium-kiosk" 2>/dev/null || true
fi

echo
echo "Fertig. Token für die Fernbedienung (KIOSK_TOKEN in der .env auf docker1):"
python3 -c "import json;print('  ' + json.load(open('$KIOSK_DIR/config.json'))['token'])"
echo
echo "Jetzt neu starten:  sudo reboot"
echo "Danach holt sich der Pi Änderungen an kiosk/pi alle 10 Minuten selbst."
