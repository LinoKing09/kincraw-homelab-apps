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
- **Kommende Termine** aus dem Kalender direkt in der App
- **Verlauf** der letzten Importe
- Verbindung zu Google trennen / neu verbinden
- Fehler einzelner Termine brechen den Import nicht mehr ab, sondern werden aufgelistet
- **Schutz vor alten PDFs:** Ist die PDF älter („letzte Änderung“) als ein schon
  importierter Stand, warnt die Vorschau; der Import fragt vorher nach
- **Teilen statt hochladen:** Die installierte App erscheint im Teilen-Menü (Android/Chrome).
  PDF teilen → „FWL Import“ wählen → Vorschau öffnet sich direkt.
  Voraussetzung: als App installiert, und das geht nur über **HTTPS**
  (bei Aufruf über `http://<IP>` bietet der Browser das Teilen-Ziel nicht an)
- **Erinnerung per Gotify** X Tage vor dem „ausgefüllt bis“-Datum (Karte „Erinnerung“,
  Standard 7 und 2 Tage vorher, ab 17 Uhr, jede Schwelle nur einmal)

## Projektstruktur

```
app.py        Flask-Routen (Seiten, API, Login)
gcal.py       Google-Kalender: Soll-Zustand, Abgleich (Plan), Ausführen
parser.py     PDF -> Daten (auch mehrseitige PDFs)
history.py    Import-Verlauf
config.py     Pfade & Einstellungen (per Umgebungsvariable änderbar)
templates/    Oberfläche (index.html = alles Design + Frontend-Logik)
static/       Icons, Manifest, Service Worker
notify.py     Gotify-Erinnerung (Einstellungen + Hintergrund-Check)
data/         client_secret.json, token.json, history.json, notify.json, shared/  (persistent, nicht ins Image)
```

## Einstellungen (Umgebungsvariablen in compose.yaml)

| Variable | Standard |
|---|---|
| `BWHV_CALENDAR_NAME` | `BWHV Schiri FWL` |
| `BWHV_MARKER_LABEL` | `FWL fertig` |
| `BWHV_PORT` | `5000` |
| `BWHV_TIMEZONE` | `Europe/Berlin` |
| `BWHV_APP_URL` | – (optional: Link, der beim Antippen der Gotify-Nachricht öffnet) |
| `BWHV_GOTIFY_URL` / `BWHV_GOTIFY_TOKEN` | – (optional; sonst in der Oberfläche einstellen) |

## Design anpassen

Alles in `templates/index.html`; Farben zentral oben im `:root`-Block.
Die Templates sind im Image. Änderung auf `main` pushen → GitHub baut ein neues Image → docker1 übernimmt es innerhalb von 15 Minuten (sofort: `homelab-update`). Siehe [Haupt-README](../README.md).

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/
```
