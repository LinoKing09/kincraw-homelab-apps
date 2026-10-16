# Setup auf Proxmox (Kurzfassung)

> **Aktueller Stand:** Die App läuft inzwischen zusammen mit den anderen Diensten im LXC **docker1** und kommt als fertiges Image aus GitHub. Für den laufenden Betrieb gilt das [Haupt-README](../README.md). Diese Anleitung beschreibt die ursprüngliche Einzel-Installation und ist nur noch für eine Neuinstallation von Grund auf relevant.

Die App läuft in einem schlanken **Debian-LXC-Container mit Docker**.
Ressourcenbedarf: 1 Kern, 512 MB RAM, 4 GB Disk reichen locker.

## 1. LXC anlegen (Proxmox-Host)

Vorlage holen – der genaue Dateiname ändert sich mit jeder Version, daher erst nachsehen und unten einsetzen:

```bash
pveam update
pveam available --section system | grep debian
pveam download local debian-13-standard_13.1-2_amd64.tar.zst   # Namen aus der Liste übernehmen
```

Container erstellen – **wichtig sind `nesting=1,keyctl=1`**, sonst läuft Docker im unprivilegierten LXC nicht:

```bash
pct create 120 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname bwhv-sync --cores 1 --memory 512 --swap 256 \
  --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1,keyctl=1 \
  --onboot 1 --password
pct start 120
```

Per GUI genauso möglich: *Create CT* → Debian-Vorlage → unter **Options → Features** `Nesting` und `keyctl` anhaken, **Start at boot** aktivieren.

> Tipp: Im Router eine **feste IP (DHCP-Reservierung)** für den Container vergeben – die App-Adresse auf dem Handy-Icon ändert sich sonst.

## 2. Projekt in den Container kopieren

Vom PC aus (IP des Containers einsetzen):

```bash
scp bwhv-sperrzeiten-sync.zip root@<LXC-IP>:/opt/
```

Im Container (`pct enter 120` auf dem Host, oder per SSH):

```bash
apt-get update && apt-get install -y unzip
cd /opt && unzip bwhv-sperrzeiten-sync.zip && cd bwhv-sync
mkdir -p data
```

## 3. Google-Zugangsdaten ablegen

**Umzug vom Pi:** `client_secret.json` **und** `token.json` vom Pi nach `/opt/bwhv-sync/data/` kopieren – dann ist keine neue Anmeldung nötig:

```bash
scp pi@<PI-IP>:~/…/bwhv-sync/{client_secret.json,token.json} root@<LXC-IP>:/opt/bwhv-sync/data/
```

**Neu eingerichtet:** nur `client_secret.json` (OAuth-Client-Typ *„TV und Geräte mit eingeschränkter Eingabe“*) nach `data/` legen, Anmeldung später per Code in der App.

## 4. Installieren & starten

```bash
bash scripts/install-lxc.sh
```

Das Skript installiert Docker (falls nötig), baut das Image und startet den Container.
Danach im Handy-Browser `http://<LXC-IP>:5000` öffnen → **Zum Startbildschirm hinzufügen** (altes Icon vorher löschen).

Autostart ist doppelt abgesichert: LXC mit `--onboot 1`, Container mit `restart: unless-stopped`.

## Alltag

| Was | Befehl (in `/opt/bwhv-sync`) |
|---|---|
| Logs ansehen | `docker compose logs -f` |
| Neu starten | `docker compose restart` |
| Update einspielen | passiert automatisch über GitHub (siehe Haupt-README); sofort: `homelab-update` |
| Anderer Port | `BWHV_PORT=8080 docker compose up -d` |
| Status | `docker compose ps` (zeigt auch den Healthcheck) |

**Backup:** Proxmox-Backup (vzdump) des LXC sichert alles. Wirklich wichtig ist nur der Ordner `data/`.

**Hinweis:** Wer Docker lieber nicht im LXC betreibt, kann dieselben Schritte 2–4 1:1 in einer kleinen Debian-VM ausführen.
