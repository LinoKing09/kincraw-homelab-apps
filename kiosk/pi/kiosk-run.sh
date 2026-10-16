#!/usr/bin/env bash
# Startet kiosk.py direkt aus dem Git-Repo und hält es aktuell.
#
#  * Vor jedem Start: git pull
#  * Alle 10 Minuten: nachsehen, ob es auf GitHub Änderungen unter kiosk/pi gibt.
#    Wenn ja: kiosk.py sauber beenden (SIGTERM schließt auch Chromium),
#    aktualisieren und neu starten.
#  * Stürzt kiosk.py ab, wird es nach 5 s neu gestartet.
#
# Wird von labwc beim Anmelden gestartet (siehe install.sh).
# config.json (Ansichten, Token) liegt außerhalb des Repos in ~/kiosk.

REPO="${KIOSK_REPO:-$HOME/kincraw-homelab-apps}"
export KIOSK_DIR="${KIOSK_DIR:-$HOME/kiosk}"
LOG="$KIOSK_DIR/kiosk.log"
CHECK_EVERY="${KIOSK_UPDATE_CHECK:-600}"   # Sekunden
BRANCH=main

mkdir -p "$KIOSK_DIR"
log() { echo "$(date '+%F %T') [kiosk-run] $*" >> "$LOG"; }

# Log nicht endlos wachsen lassen (> 5 MB -> eine alte Kopie behalten)
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ]; then
  mv "$LOG" "$LOG.1"
fi

update() {
  if git -C "$REPO" pull --ff-only -q origin "$BRANCH" >> "$LOG" 2>&1; then
    log "Stand: $(git -C "$REPO" log -1 --format='%h %s')"
  else
    log "git pull fehlgeschlagen – starte mit dem vorhandenen Stand"
  fi
}

changes_waiting() {
  git -C "$REPO" fetch -q origin "$BRANCH" 2>> "$LOG" || return 1
  ! git -C "$REPO" diff --quiet HEAD "origin/$BRANCH" -- kiosk/pi
}

PID=""
trap '[ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null; wait; exit 0' TERM INT

[ -z "${KIOSK_JUST_UPDATED:-}" ] && update
unset KIOSK_JUST_UPDATED
while true; do
  python3 "$REPO/kiosk/pi/kiosk.py" >> "$LOG" 2>&1 &
  PID=$!
  log "kiosk.py gestartet (PID $PID)"

  waited=0
  while kill -0 "$PID" 2>/dev/null; do
    sleep 10
    waited=$((waited + 10))
    if [ "$waited" -ge "$CHECK_EVERY" ]; then
      waited=0
      if changes_waiting; then
        log "Update gefunden – kiosk.py wird neu gestartet"
        kill -TERM "$PID" 2>/dev/null
        wait "$PID" 2>/dev/null
        update
        # Neu einlesen, damit auch Änderungen an diesem Skript selbst greifen
        KIOSK_JUST_UPDATED=1 exec "$REPO/kiosk/pi/kiosk-run.sh"
      fi
    fi
  done

  wait "$PID" 2>/dev/null
  log "kiosk.py beendet (Code $?) – Neustart in 5 s"
  sleep 5
  update
done
