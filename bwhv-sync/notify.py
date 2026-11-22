# -*- coding: utf-8 -*-
"""Erinnerung per Gotify, bevor die Freiwunschliste ausläuft.

Einstellungen werden in der Oberfläche gepflegt und liegen in data/notify.json
(Fallback: Umgebungsvariablen BWHV_GOTIFY_URL / BWHV_GOTIFY_TOKEN).

    {"settings": {"url": "...", "token": "...", "days": [7, 2]},
     "sent": {"bis_date": "2026-12-31", "days": [7]}}

Pro "ausgefüllt bis"-Datum wird jede Schwelle höchstens einmal gemeldet.
Ein neuer Import mit anderem Datum setzt das automatisch zurück.
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime

import requests

import config
import history

log = logging.getLogger("bwhv-notify")
_lock = threading.Lock()

DEFAULT_DAYS = [7, 2]
MAX_DAYS = 60
SEND_FROM_HOUR = 17          # nicht nachts erinnern
CHECK_EVERY_S = 15 * 60
TIMEOUT = 10


class NotifyError(RuntimeError):
    """Fehler mit verständlicher Meldung für die Oberfläche."""


def _path() -> str:
    return os.path.join(config.DATA_DIR, "notify.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    config.ensure_data_dir()
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def settings() -> dict:
    s = _load().get("settings") or {}
    days = s.get("days")
    return {
        "url": (s.get("url") or os.environ.get("BWHV_GOTIFY_URL") or "").strip(),
        "token": (s.get("token") or os.environ.get("BWHV_GOTIFY_TOKEN") or "").strip(),
        "days": days if isinstance(days, list) else list(DEFAULT_DAYS),
    }


def public_settings() -> dict:
    """Für die Oberfläche – ohne Token."""
    s = settings()
    return {"configured": bool(s["url"] and s["token"]), "url": s["url"],
            "token_set": bool(s["token"]), "days": s["days"], "from_hour": SEND_FROM_HOUR}


def parse_days(value) -> list:
    """'7, 2' oder [7, 2] -> [7, 2] (absteigend, ohne Doppelte)."""
    parts = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    out = set()
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        if not p.isdigit() or int(p) > MAX_DAYS:
            raise NotifyError(f"Tage bitte als Zahlen von 0 bis {MAX_DAYS} angeben, z. B. „7, 2“.")
        out.add(int(p))
    if not out:
        raise NotifyError("Mindestens einen Tag angeben, z. B. „7“.")
    if len(out) > 5:
        raise NotifyError("Höchstens 5 Erinnerungen.")
    return sorted(out, reverse=True)


def save_settings(p: dict) -> dict:
    with _lock:
        d = _load()
        cur = settings()
        url = str(p.get("url", cur["url"]) or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise NotifyError("Die Gotify-Adresse muss mit http:// oder https:// beginnen.")
        token = str(p.get("token") or "").strip() or cur["token"]   # leer = bisheriges behalten
        if not url:
            token = ""                                               # Adresse leer = ausschalten
        days = parse_days(p["days"]) if "days" in p else cur["days"]
        d["settings"] = {"url": url, "token": token, "days": days}
        _save(d)
    return public_settings()


def send(title: str, message: str, priority: int = 6):
    s = settings()
    if not (s["url"] and s["token"]):
        raise NotifyError("Gotify ist noch nicht eingerichtet.")
    body = {"title": title, "message": message, "priority": priority}
    app_url = os.environ.get("BWHV_APP_URL")
    if app_url:
        body["extras"] = {"client::notification": {"click": {"url": app_url}}}
    try:
        r = requests.post(f"{s['url']}/message", json=body,
                          headers={"X-Gotify-Key": s["token"]}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise NotifyError(f"Gotify nicht erreichbar ({e.__class__.__name__}). Adresse prüfen.") from e
    if r.status_code in (401, 403):
        raise NotifyError("Gotify lehnt das Token ab. Es muss ein App-Token sein (nicht das Client-Token).")
    if r.status_code >= 400:
        raise NotifyError(f"Gotify meldet Fehler {r.status_code}.")


def _text(bis: date, days_left: int) -> str:
    d = bis.strftime("%d.%m.%Y")
    if days_left < 0:
        return f"Die Freiwunschliste ist seit dem {d} nicht mehr ausgefüllt."
    if days_left == 0:
        return f"Die Freiwunschliste ist nur noch bis heute ({d}) ausgefüllt."
    if days_left == 1:
        return f"Die Freiwunschliste ist nur noch bis morgen ({d}) ausgefüllt."
    return f"Die Freiwunschliste ist nur noch {days_left} Tage ausgefüllt (bis {d})."


def check(now: datetime = None):
    """Schickt bei Bedarf eine Erinnerung. Gibt den Text zurück oder None."""
    s = settings()
    last = history.last()
    if not (s["url"] and s["token"]) or not last or not last.get("bis_date"):
        return None
    now = now or datetime.now(config.TZ)
    if now.hour < SEND_FROM_HOUR:
        return None
    bis = date.fromisoformat(last["bis_date"])
    days_left = (bis - now.date()).days

    with _lock:
        d = _load()
        sent = d.get("sent") or {}
        if sent.get("bis_date") != bis.isoformat():
            sent = {"bis_date": bis.isoformat(), "days": []}
        due = [t for t in s["days"] if days_left <= t and t not in sent["days"]]
        if not due:
            return None
        text = _text(bis, days_left) + "\nZeit, sie im Portal zu verlängern und neu zu importieren."
        send("Freiwunschliste ausfüllen", text)
        # Alle schon überschrittenen Schwellen auf einmal abhaken -> nur eine Nachricht
        sent["days"] = sorted(set(sent["days"]) | set(due), reverse=True)
        d["sent"] = sent
        _save(d)
    log.info("Erinnerung gesendet: %s", text.splitlines()[0])
    return text


def _loop():
    time.sleep(60)
    while True:
        try:
            check()
        except NotifyError as e:
            log.warning("Erinnerung fehlgeschlagen: %s", e)
        except Exception:  # noqa: BLE001 – Hintergrund-Thread darf nie sterben
            log.exception("Erinnerung: unerwarteter Fehler")
        time.sleep(CHECK_EVERY_S)


def start_background():
    if os.environ.get("BWHV_DISABLE_SCHEDULER") == "1":
        return
    threading.Thread(target=_loop, daemon=True, name="notify").start()
