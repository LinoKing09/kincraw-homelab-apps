# Kiosk + Fernbedienung

Zwei Teile:

| Teil | Wo | Aufgabe |
| --- | --- | --- |
| `pi/kiosk.py` | Raspberry Pi am Bildschirm | Startet Chromium, zeigt die HA-Ansichten nacheinander, Display an/aus, API auf Port 8765 |
| `remote/` | Docker auf docker1 (`/opt/stack`) | Fernbedienungs-Seite fürs Handy, leitet Befehle an den Pi weiter |

Auf dem Pi läuft nur `kiosk.py` – kein Docker, kein PM2. labwc startet beim Anmelden
`pi/kiosk-run.sh`, dadurch ist die Wayland-Umgebung (`XDG_RUNTIME_DIR`) automatisch vorhanden.

## Updates

| Teil | Wie kommt eine Änderung an? |
| --- | --- |
| `remote/` | Push auf `main` → GitHub baut `ghcr.io/linoking09/kiosk-remote` → docker1 holt es innerhalb von 15 Min. (siehe [Haupt-README](../README.md)) |
| `pi/` | Der Pi hat das Repo (nur `kiosk/pi`) unter `~/kincraw-homelab-apps`. `kiosk-run.sh` macht vor jedem Start `git pull` und sieht alle 10 Min. nach Änderungen unter `kiosk/pi`. Gibt es welche, wird kiosk.py sauber beendet und mit dem neuen Stand gestartet. |

Der Pi greift mit einem eigenen **Deploy key** (nur lesen, nur dieses Repo) auf GitHub zu.
Klappt ein `git pull` nicht (z. B. kein Netz), startet der Kiosk einfach mit dem vorhandenen Stand.

## 1. Pi einrichten

Voraussetzung: Raspberry Pi OS (Desktop), Benutzer `lines`, Autologin in den Desktop, feste IP.

Nur `pi/install.sh` wird gebraucht – den Rest holt es sich selbst aus GitHub:

```bash
nano ~/install.sh      # Inhalt von pi/install.sh einfügen
bash ~/install.sh
```

Das Skript
1. installiert git, python3-websocket, wlopm (und Chromium, falls nötig),
2. erzeugt einen SSH-Schlüssel und zeigt ihn an → auf GitHub unter
   *Repo → Settings → Deploy keys → Add deploy key* eintragen (**ohne** Schreibrecht),
3. klont `kiosk/pi` nach `~/kincraw-homelab-apps`,
4. ersetzt den alten Autostart-Eintrag durch `kiosk-run.sh`
   (eine alte `~/kiosk/kiosk.py` wird zu `kiosk.py.alt`, `config.json` bleibt),
5. zeigt am Ende das **Token** an – das brauchst du für die Fernbedienung.

Ansichten anpassen: entweder bequem in der Fernbedienung (**Bearbeiten**) oder in
`~/kiosk/config.json` (liegt bewusst **nicht** im Repo). Dort stehen auch:

| Schlüssel | Bedeutung |
| --- | --- |
| `display_output` | `"*"` oder z. B. `"HDMI-A-2"` für `wlopm` |
| `pir_pin` | GPIO-Nummer des PIR-Sensors (z. B. `17`), `null` = kein PIR |
| `pir_timeout` | Sekunden ohne Bewegung, bis das Display ausgeht |
| `full_reload_hours` | Seite alle X Stunden komplett neu laden |

Die Datei wird nur beim Start von `kiosk.py` gelesen – nach Änderungen per Hand also `sudo reboot`.
Änderungen über die Fernbedienung speichert das Skript selbst.

**Home-Assistant-Anmeldung:** Beim ersten Start einmal mit Tastatur in Chromium anmelden und
„Angemeldet bleiben“ wählen – der Login liegt in `~/.chromium-kiosk` und bleibt erhalten.
Alternativ in HA `trusted_networks` mit der **neuen** Pi-IP eintragen.

| Was | Befehl auf dem Pi |
| --- | --- |
| Log (Kiosk + Updates) | `tail -f ~/kiosk/kiosk.log` |
| Welcher Stand läuft? | `git -C ~/kincraw-homelab-apps log -1 --oneline` |
| Update sofort | `pkill -f kiosk/pi/kiosk.py` (Wrapper zieht beim Neustart den neuen Stand) |

## 2. Fernbedienung auf docker1

```bash
mkdir -p /opt/stack/kiosk-remote && cd /opt/stack/kiosk-remote
nano .env        # KIOSK_URL=http://<pi-ip>:8765  und  KIOSK_TOKEN=<Token vom Pi>
```

Vorlage: [`remote/.env.example`](remote/.env.example). Der Dienst `kiosk-remote` steht schon in
[`deploy/compose.yaml`](../deploy/compose.yaml). Danach:

```bash
cd /opt/stack
docker compose up -d kiosk-remote
```

Aufrufen: `http://<docker1-ip>:8090` – im Handy über „Zum Startbildschirm hinzufügen“ als App ablegen.

## Was die Fernbedienung kann

- Live-Vorschau des Bildschirms mit Restzeit der aktuellen Ansicht
- Zurück / Pause–Weiter / Vor, Ansicht direkt antippen
- Display an/aus
- Ansichten hinzufügen, umbenennen, sortieren, löschen, eigene Anzeigedauer pro Ansicht
- Beliebige URL für 1 min … 1 h oder dauerhaft einblenden, danach zurück zur Rotation
- Anzeigedauer, Zoom, HA-Kopfzeile ausblenden, Seite neu laden, Browser neu starten

Ansichten vom selben HA-Dashboard wechseln ohne Neuladen (HA-interne Navigation) – kein Flackern.

## Sicherheit

Die API des Pi ist nur mit Token nutzbar; das Token liegt nur auf dem Pi und in der `.env` auf docker1.
Chromiums Debug-Port 9222 ist nur lokal auf dem Pi erreichbar.
