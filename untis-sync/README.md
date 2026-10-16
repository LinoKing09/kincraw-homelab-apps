# Untis Sync – Stundenplan → Google Kalender

Holt deinen Stundenplan per WebUntis und trägt die Stunden in deinen Google
Kalender ein – mit dem Event-Label **„Schule“**. Läuft als eigener Docker-
Container neben BWHV FWL Sync und synchronisiert automatisch (Standard: alle
2 Stunden, 4 Wochen im Voraus). Über die Web-Oberfläche kannst du jederzeit
manuell syncen.

## Was die App macht

- **Nur eigene Termine:** Jeder Termin trägt eine unsichtbare Kennung
  (`untis_managed`). Andere Einträge in deinem Kalender werden nie angefasst.
- **Änderungen statt Duplikate:** Verschobene Stunden, Raum- oder
  Lehrerwechsel aktualisieren den bestehenden Termin.
- **Entfall:** bleibt als „ENTFALL – Fach“ stehen und blockiert keine Zeit
  (oder wird mit `CANCELLED_MODE=delete` entfernt).
- **Vertretung / Prüfung:** bekommen ein Präfix im Titel, Details stehen in
  der Beschreibung.
- **Doppelstunden** werden zu einem Termin zusammengefasst.
- **Keine Erinnerungen** pro Stunde (sonst klingelt das Handy 6× am Tag) –
  per `REMINDER_MINUTES` einstellbar.
- Vergangene Tage werden nie verändert.

## Stunden und Tage ausblenden

In der Wochenansicht (◀ ▶ zum Blättern):

- **Tag antippen**: ganzer Tag kommt nicht in den Kalender (z. B. Ausflug,
  krank). Nochmal antippen blendet ihn wieder ein.
- **Stunde antippen**:
  - *Nur diesmal entfernen*: nur dieser eine Termin.
  - *Dauerhaft entfernen*: jede Woche am selben Wochentag zur selben
    Uhrzeit mit demselben Fach. Bleibt grau sichtbar (∞) und lässt sich
    „nur diesmal“ oder „dauerhaft“ wieder hinzufügen.

Ausgeblendetes wird sofort aus dem Kalender gelöscht und auch vom Auto-Sync
nicht wieder eingetragen. Gespeichert in `data/exclusions.json`.

## Einrichtung

### 1) Google-Zugang

Du kannst das **gleiche Google-Cloud-Projekt** wie für BWHV FWL Sync nutzen –
Calendar API ist dort schon aktiv. Einfach die `client_secret.json`
(Typ „TV und Geräte mit eingeschränkter Eingabe“) kopieren:

```bash
cp bwhv-sync/data/client_secret.json untis-sync/client_secret.json   # im Stack-Ordner auf docker1
```

### 2) Label prüfen

Das Label **„Schule“** muss auf dem Kalender existieren, in den importiert
wird. Standard ist dein Hauptkalender (`GCAL_CALENDAR_ID=primary`). Hast du
das Label auf einem anderen Kalender angelegt, trage dessen Kalender-ID ein
(Google Kalender → Einstellungen des Kalenders → „Kalender-ID“).

### 3) Zugangsdaten

```bash
cp .env.example .env
nano .env
```

Server und Schulname stehen in der Adresse, mit der du dich bei WebUntis im
Browser anmeldest, z. B.
`https://nessa.webuntis.com/WebUntis/?school=gym-beispiel` →
`UNTIS_SERVER=nessa.webuntis.com`, `UNTIS_SCHOOL=gym-beispiel`.

> Die `.env` enthält dein WebUntis-Passwort – nicht in Git o. Ä. hochladen.

### 4) Starten

Das Image kommt fertig aus GitHub (`ghcr.io/linoking09/untis-sync`), siehe
[Haupt-README](../README.md). Im Stack-Ordner auf docker1:

```bash
docker compose up -d untis-sync
docker compose logs -f untis-sync
```

Dann `http://<IP-des-Servers>:5001` öffnen → **Mit Google verbinden** → Code
auf google.com/device eingeben → **Jetzt synchronisieren**. Ab dann läuft der
Sync automatisch. Auf dem Handy über „Zum Startbildschirm hinzufügen“ als App
installierbar.

## Wenn etwas nicht klappt

| Meldung | Ursache |
|---|---|
| „Benutzername oder Passwort falsch“ | Zugangsdaten in `.env` prüfen |
| „keine gültige Antwort“ | `UNTIS_SERVER`/`UNTIS_SCHOOL` falsch geschrieben |
| Login geht im Browser nur über Microsoft/IServ o. Ä. | Deine Schule nutzt SSO – dann funktioniert der Login mit Benutzername/Passwort nicht direkt |
| „Label … nicht gefunden“ | Label liegt auf einem anderen Kalender oder heißt anders → `GCAL_CALENDAR_ID` / `GCAL_LABEL_NAME` |

Nach Änderungen an der `.env`: `docker compose up -d` (Neustart reicht).
