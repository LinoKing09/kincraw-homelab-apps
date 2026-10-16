# KingCraw Homelab Apps

Selbst geschriebene Apps für das Homelab der Familie KingCraw. Sie laufen als
Docker-Container im LXC **docker1** (CT 301) auf dem Proxmox-Host **server1**.

| App | Ordner | Port | Zweck |
|---|---|---|---|
| BWHV FWL Sync | [`bwhv-sync/`](bwhv-sync/README.md) | 5000 | Freiwunschliste (PDF) → Google Kalender „BWHV Schiri FWL" |
| Untis Sync | [`untis-sync/`](untis-sync/README.md) | 5001 | WebUntis-Stundenplan → Google Kalender (Label „Schule") |
| Kiosk | *folgt* | – | Fernbedienungsseite (Container) + Kiosk-Skript auf dem Raspberry Pi |

## Wie Updates auf den Server kommen

```
Änderung auf main pushen
        │
        ▼
GitHub Actions ── Tests ── baut Images ──► ghcr.io/linoking09/<app>:latest
                                                   │
docker1: Timer "homelab-update" (alle 15 Min.) ◄───┘
        docker compose pull  →  docker compose up -d
        (neu gestartet werden nur Container mit neuem Image)
```

- Workflow: [`.github/workflows/build.yml`](.github/workflows/build.yml)
- Jedes Image bekommt zusätzlich den Commit-Hash als Tag, damit man auf einen
  alten Stand zurück kann (siehe unten).
- Schlagen die Tests fehl, wird **kein** Image gebaut und auf dem Server
  ändert sich nichts.

## Einmalige Einrichtung auf docker1

1. Auf GitHub ein **Token (classic)** mit **nur** dem Recht `read:packages`
   anlegen: *Settings → Developer settings → Personal access tokens → Tokens (classic)*.
2. Den Inhalt von [`deploy/setup-docker1.sh`](deploy/setup-docker1.sh) im
   Stack-Ordner auf docker1 als `setup-docker1.sh` speichern und ausführen:
   ```bash
   bash setup-docker1.sh
   ```
   Das Skript loggt sich bei ghcr.io ein, sichert die alte `compose.yaml`,
   ersetzt sie durch die Image-Version und richtet den Update-Timer ein.

Die Datenordner (`bwhv-sync/data`, `untis-sync/data`, `untis-sync/.env`,
`untis-sync/client_secret.json`) bleiben unverändert auf dem Server liegen.

## Alltag auf docker1

| Was | Befehl |
|---|---|
| Sofort aktualisieren | `homelab-update` |
| Wann läuft der nächste Check? | `systemctl list-timers homelab-update.timer` |
| Update-Log | `journalctl -u homelab-update -n 50` |
| Logs einer App | `docker compose logs -f bwhv-sync` |
| Welche Version läuft? | `docker inspect bwhv-sync --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'` |

### Zurück auf einen alten Stand

In der `compose.yaml` statt `:latest` den Commit-Hash eintragen, z. B.
`ghcr.io/linoking09/bwhv-sync:3f2a…`, dann `docker compose up -d`. Solange dort
ein fester Hash steht, ignoriert der Timer neue Versionen dieser App.

## Was nie ins Repo gehört

`data/`, `.env`, `client_secret.json`, `token.json`, `secret_key` – alles
Zugangsdaten oder Laufzeitdaten. Die [`.gitignore`](.gitignore) schließt sie aus.
**Backup** dieser Dateien läuft über Proxmox (vzdump von CT 301), nicht über Git.

## Lokal entwickeln

```bash
cd bwhv-sync
pip install -r requirements.txt pytest
pytest tests/
```
