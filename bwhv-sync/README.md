# BWHV FWL Sync

Web-App (PWA), die die BWHV-Freiwunschliste (PDF) einliest und mit dem
Google-Kalender **„BWHV Schiri FWL“** abgleicht. Installation: siehe **SETUP.md**.

## Zuordnung PDF → Kalender

| PDF-Abschnitt | Kalender-Termin |
|---|---|
| Generelle Sperrzeiten | wöchentlich bis zum „ausgefüllt bis“-Datum, „Generelle Sperrzeit“ |
| Sperrzeiträume | ganztägig, „Langer Sperrzeitraum“ |
| weitere Sperrtermine | mit Uhrzeit, „Sperrtermin (Grund)“ |
| „ausgefüllt bis“-Datum | ganztägig „FWL - aktuellster Punkt“ mit Label „FWL fertig“ (wird jedes Mal neu angelegt) |

Abgleich: unverändert bleibt unverändert, Geändertes wird aktualisiert, Neues angelegt,
nicht mehr Vorhandenes gelöscht – aber **nur Termine, die die App selbst angelegt hat**
(unsichtbare private Kennung). Eigene Einträge werden nie angefasst.

## Funktionen der Oberfläche

- **Vorschau** vor dem Import: zeigt genau, was neu / geändert (mit altem Wert) / gelöscht / gleich bleibt
- **Datums-Schnellwahl** („wie zuletzt“, +4 Wochen, Monatsende) und Warnung bei Datum in der Vergangenheit
- **Link „Phoenix2 öffnen“** im Import-Bereich (`BWHV_PORTAL_URL`)
- **Kommende Termine** aus dem Kalender direkt in der App – Einzeltermine nach Datum,
  die wöchentlichen Sperrzeiten einmal zusammengefasst; standardmäßig zugeklappt
  (die ersten 3 sichtbar, der Rest blendet aus, „Alle … anzeigen“ klappt auf)
- **Verlauf** der letzten Importe inkl. Stand der PDF
- **Schutz vor alten PDFs:** Ist die PDF älter („letzte Änderung“) als ein schon
  importierter Stand, warnt die Vorschau; der Import fragt vorher nach
- **Erinnerung per Gotify** X Tage vor dem „ausgefüllt bis“-Datum (Karte „Erinnerung“,
  Standard 7 und 2 Tage vorher, ab 17 Uhr, jede Schwelle nur einmal)
- **Teilen vom Handy** (Android/Chrome): PDF im Portal herunterladen → Teilen → FWL Import.
  Voraussetzung: als App installiert, und das geht nur über **HTTPS**
  (bei Aufruf über `http://<IP>` bietet der Browser das Teilen-Ziel nicht an)
- **Passwortschutz** über `BWHV_PASSWORD` (Anmeldung bleibt `BWHV_LOGIN_DAYS` Tage gültig,
  nach 5 Fehlversuchen 5 Minuten Sperre)
- **Abgelaufene Google-Anmeldung** wird erkannt: statt eines Fehlers erscheint „Neu verbinden“
- Verbindung zu Google trennen / neu verbinden
- Fehler einzelner Termine brechen den Import nicht ab, sondern werden aufgelistet

## Projektstruktur

```
app.py        Flask-Routen (Seiten, API, Login, Teilen)
gcal.py       Google-Kalender: Soll-Zustand, Abgleich (Plan), Ausführen
parser.py     PDF -> Daten (auch mehrseitige PDFs)
history.py    Import-Verlauf
notify.py     Gotify-Erinnerung (Einstellungen + Hintergrund-Check)
config.py     Pfade & Einstellungen (per Umgebungsvariable änderbar)
templates/    Oberfläche (index.html = alles Design + Frontend-Logik, login.html, auth.html)
static/       Icons, Manifest, Service Worker
data/         client_secret.json, token.json, history.json, notify.json, secret_key, shared/
              (persistent, nicht ins Image)
```

## Einstellungen (Datei `.env`)

Auf docker1 liegt sie als **`/opt/stack/bwhv-sync/.env`** (Vorlage: `.env.example`);
`compose.yaml` liest sie per `env_file` ein. Übernehmen: `docker compose up -d bwhv-sync`.
Prüfen, was ankommt: `docker compose config bwhv-sync | grep BWHV`.

| Variable | Standard |
|---|---|
| `BWHV_PASSWORD` | leer = ohne Anmeldung (nur im Heimnetz!) |
| `BWHV_LOGIN_DAYS` | `90` |
| `BWHV_PORTAL_URL` | `https://bwhv.it4sport.de/auth/login` (Link „Phoenix2 öffnen“, leer = ausblenden) |
| `BWHV_CALENDAR_NAME` | `BWHV Schiri FWL` |
| `BWHV_MARKER_LABEL` | `FWL fertig` |
| `BWHV_TIMEZONE` | `Europe/Berlin` |
| `BWHV_APP_URL` | leer (optional: Adresse der App – Antippen der Gotify-Nachricht öffnet sie) |
| `BWHV_GOTIFY_URL` / `BWHV_GOTIFY_TOKEN` | leer (optional; sonst in der Oberfläche einstellen) |

Enthält das Passwort ein `$`, den Wert in einfache Anführungszeichen setzen: `BWHV_PASSWORD='ab$cd'`.

## Design anpassen

Alles in `templates/index.html`; Farben zentral oben im `:root`-Block.
Die Templates sind im Image. Änderung auf `main` pushen → GitHub baut ein neues Image → docker1 übernimmt es innerhalb von 15 Minuten (sofort: `homelab-update`). Siehe [Haupt-README](../README.md).

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/
```
