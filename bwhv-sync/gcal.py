# -*- coding: utf-8 -*-
"""
Google-Calendar-Anbindung für den BWHV-FWL-Sync.

Jeder von der App erzeugte Termin trägt private extendedProperties
(in der Kalender-Oberfläche unsichtbar):
    bwhv_managed  = "1"   -> stammt von dieser App (nur solche werden je gelöscht)
    bwhv_category = generell | sperrzeitraum | sperrtermin | marker
    bwhv_sync_key = stabiler Schlüssel des "logischen" Termins
    bwhv_hash     = Hash über die inhaltsrelevanten Felder

Ablauf:  build_desired()  -> was laut PDF existieren soll
         compute_plan()   -> Abgleich mit dem Kalender (neu / ändern / gleich / löschen)
         apply_plan()     -> Plan ausführen
Die Vorschau in der Oberfläche zeigt einfach den Plan, ohne apply_plan().
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
from parser import WEEKDAY_NAMES

SCOPES = ["https://www.googleapis.com/auth/calendar"]
RRULE_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MARKER_COLOR_ID_FALLBACK = "6"  # Tangerine, nur falls das Label nicht gefunden wird

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
HTTP_TIMEOUT = 15

_calendar_id_cache = {}


class SyncError(RuntimeError):
    pass


# ================================================================ Auth ====

def _save_creds(creds: Credentials):
    config.ensure_data_dir()
    with open(config.TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def _load_creds():
    if not os.path.exists(config.TOKEN_PATH) or os.path.getsize(config.TOKEN_PATH) == 0:
        return None
    try:
        return Credentials.from_authorized_user_file(config.TOKEN_PATH, SCOPES)
    except Exception:
        return None


def has_client_secret() -> bool:
    return os.path.exists(config.CLIENT_SECRET_PATH)


def is_authenticated() -> bool:
    creds = _load_creds()
    if not creds:
        return False
    if creds.valid:
        return True
    if creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(creds)
            return True
        except Exception:
            return False
    return False


def _load_client_config():
    if not has_client_secret():
        raise FileNotFoundError(
            "client_secret.json fehlt im Datenordner. Siehe SETUP.md, Abschnitt 'Google Cloud'.")
    with open(config.CLIENT_SECRET_PATH) as f:
        data = json.load(f)
    cfg = data.get("installed") or data.get("web")
    return cfg["client_id"], cfg["client_secret"]


def start_device_flow() -> dict:
    client_id, _ = _load_client_config()
    resp = requests.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": " ".join(SCOPES)},
                         timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def poll_device_flow(device_code: str) -> dict:
    client_id, client_secret = _load_client_config()
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }, timeout=HTTP_TIMEOUT)
    data = resp.json()
    if resp.status_code == 200:
        _save_creds(Credentials(
            token=data["access_token"], refresh_token=data.get("refresh_token"),
            token_uri=TOKEN_URL, client_id=client_id, client_secret=client_secret, scopes=SCOPES))
        return {"status": "ok"}
    error = data.get("error")
    if error in ("authorization_pending", "slow_down"):
        return {"status": "pending", "slow_down": error == "slow_down"}
    if error == "expired_token":
        return {"status": "expired"}
    if error == "access_denied":
        return {"status": "error", "error": "Zugriff abgelehnt (Testnutzer eingetragen?)"}
    return {"status": "error", "error": error or "unbekannter Fehler"}


def disconnect():
    """Token bei Google widerrufen und lokal löschen."""
    creds = _load_creds()
    if creds:
        token = creds.refresh_token or creds.token
        try:
            requests.post(REVOKE_URL, params={"token": token}, timeout=HTTP_TIMEOUT)
        except Exception:
            pass
    if os.path.exists(config.TOKEN_PATH):
        os.remove(config.TOKEN_PATH)
    _calendar_id_cache.clear()


def get_service():
    creds = _load_creds()
    if not creds:
        raise SyncError("Nicht mit Google verbunden.")
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)
    # static_discovery=False: aktuelles Discovery-Dokument (nötig für eventLabelId)
    return build("calendar", "v3", credentials=creds, static_discovery=False, cache_discovery=False)


# ============================================================ Calendar ====

def get_or_create_calendar_id(service) -> str:
    name = config.CALENDAR_NAME
    if name in _calendar_id_cache:
        return _calendar_id_cache[name]
    page_token = None
    while True:
        res = service.calendarList().list(pageToken=page_token).execute()
        for cal in res.get("items", []):
            if cal.get("summary") == name:
                _calendar_id_cache[name] = cal["id"]
                return cal["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    cal = service.calendars().insert(body={"summary": name, "timeZone": config.TIMEZONE}).execute()
    _calendar_id_cache[name] = cal["id"]
    return cal["id"]


def _list_managed(service, calendar_id: str) -> list:
    items, page_token = [], None
    while True:
        res = service.events().list(
            calendarId=calendar_id, privateExtendedProperty="bwhv_managed=1",
            showDeleted=False, singleEvents=False, maxResults=250, pageToken=page_token,
        ).execute()
        items.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            return items


def _get_label_id(service, calendar_id: str):
    try:
        cal = service.calendars().get(calendarId=calendar_id).execute()
    except HttpError:
        return None
    for lbl in (cal.get("labelProperties") or {}).get("eventLabels") or []:
        if lbl.get("name") == config.MARKER_LABEL_NAME:
            return lbl.get("id")
    return None


def list_upcoming(limit: int = 40) -> list:
    """Kommende, von der App verwaltete Termine (Einzelvorkommen) für die Übersicht."""
    service = get_service()
    cal_id = get_or_create_calendar_id(service)
    now = datetime.now(config.TZ)
    res = service.events().list(
        calendarId=cal_id, privateExtendedProperty="bwhv_managed=1", singleEvents=True,
        orderBy="startTime", timeMin=now.isoformat(), maxResults=limit,
    ).execute()
    out = []
    for ev in res.get("items", []):
        cat = ev.get("extendedProperties", {}).get("private", {}).get("bwhv_category", "")
        out.append({"title": ev.get("summary", ""), "when": _event_when(ev), "category": cat,
                    "start": ev["start"].get("dateTime") or ev["start"].get("date")})
    return out


# ============================================================= Helpers ====

def _hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def today() -> date:
    return datetime.now(config.TZ).date()


def _next_weekday(weekday_index: int, anchor: date) -> date:
    return anchor + timedelta(days=(weekday_index - anchor.weekday()) % 7)


def _description(last_change, extra: str = "") -> str:
    lines = [extra] if extra else []
    if last_change:
        lines.append(f"Letzte Änderung der Freiwunschliste: {last_change.strftime('%d.%m.%Y %H:%M')}")
    lines.append("(automatisch erzeugt von BWHV FWL Sync)")
    return "\n".join(lines)


def _rrule_until_utc(bis_date: date) -> str:
    local_end = datetime.combine(bis_date, dtime(23, 59, 59), tzinfo=config.TZ)
    return local_end.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _fmt_d(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _local_dt(value: str) -> datetime:
    """Zeitpunkt in config.TZ. Zeiten ohne Offset (so legt die App sie an)
    gelten als Ortszeit – unabhängig von der Zeitzone des Rechners."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=config.TZ) if dt.tzinfo is None else dt.astimezone(config.TZ)


def _event_when(ev: dict) -> str:
    s, e = ev.get("start", {}), ev.get("end", {})
    if "date" in s:
        sd = date.fromisoformat(s["date"])
        ed = date.fromisoformat(e["date"]) - timedelta(days=1)
        return _fmt_d(sd) if sd == ed else f"{_fmt_d(sd)} – {_fmt_d(ed)}"
    sdt, edt = _local_dt(s["dateTime"]), _local_dt(e["dateTime"])
    if ev.get("recurrence"):
        return f"jeden {WEEKDAY_NAMES[sdt.weekday()]}, {sdt:%H:%M}–{edt:%H:%M}"
    return f"{_fmt_d(sdt.date())}, {sdt:%H:%M}–{edt:%H:%M}"


# ========================================================== Soll-Zustand ===

def build_desired(fw, bis_date: date, anchor: date = None) -> list:
    """Liste der Termine, die laut PDF im Kalender existieren sollen."""
    anchor = anchor or today()
    until = _rrule_until_utc(bis_date)
    tz = config.TIMEZONE
    items = []

    for g in fw.generelle_sperrzeiten:
        first = _next_weekday(g.weekday_index, anchor)
        if first > bis_date:
            continue
        items.append({
            "category": "generell",
            "sync_key": f"generell-{g.weekday_abbr}",
            "hash": _hash("generell", g.weekday_abbr, g.start, g.end, until),
            "title": "Generelle Sperrzeit",
            "when": f"jeden {WEEKDAY_NAMES[g.weekday_index]}, {g.start:%H:%M}–{g.end:%H:%M} "
                    f"(bis {_fmt_d(bis_date)})",
            "sort": (0, g.weekday_index),
            "start_time": g.start, "end_time": g.end,
            "body": {
                "summary": "Generelle Sperrzeit",
                "description": _description(fw.last_change),
                "start": {"dateTime": datetime.combine(first, g.start).isoformat(), "timeZone": tz},
                "end": {"dateTime": datetime.combine(first, g.end).isoformat(), "timeZone": tz},
                "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={RRULE_WEEKDAYS[g.weekday_index]};UNTIL={until}"],
            },
        })

    for z in fw.sperrzeitraeume:
        items.append({
            "category": "sperrzeitraum",
            "sync_key": f"sperrzeitraum-{z.start_date.isoformat()}",
            "hash": _hash("sperrzeitraum", z.start_date, z.end_date),
            "title": "Langer Sperrzeitraum",
            "when": f"{_fmt_d(z.start_date)} – {_fmt_d(z.end_date)}",
            "sort": (1, z.start_date.isoformat()),
            "body": {
                "summary": "Langer Sperrzeitraum",
                "description": _description(fw.last_change),
                "start": {"date": z.start_date.isoformat()},
                "end": {"date": (z.end_date + timedelta(days=1)).isoformat()},
            },
        })

    # Schlüssel nur aus dem Datum (nie aus der Uhrzeit), damit eine geänderte
    # Uhrzeit als Aktualisierung erkannt wird. Mehrere Termine am selben Tag
    # werden nach Startzeit durchnummeriert.
    by_date = defaultdict(list)
    for t in fw.weitere_sperrtermine:
        by_date[t.the_date].append(t)
    for d, entries in by_date.items():
        entries.sort(key=lambda e: e.start)
        for i, t in enumerate(entries):
            key = f"sperrtermin-{d.isoformat()}" + ("" if len(entries) == 1 else f"-{i}")
            title = f"Sperrtermin ({t.note})" if t.note else "Sperrtermin"
            items.append({
                "category": "sperrtermin",
                "sync_key": key,
                "legacy_prefix": f"sperrtermin-{d.isoformat()}-",
                "legacy_hhmm": t.start.strftime("%H%M"),
                "hash": _hash("sperrtermin", t.the_date, t.start, t.end, t.note),
                "title": title,
                "when": f"{_fmt_d(d)}, {t.start:%H:%M}–{t.end:%H:%M}",
                "sort": (2, d.isoformat(), t.start.isoformat()),
                "body": {
                    "summary": title,
                    "description": _description(fw.last_change),
                    "start": {"dateTime": datetime.combine(d, t.start).isoformat(), "timeZone": tz},
                    "end": {"dateTime": datetime.combine(d, t.end).isoformat(), "timeZone": tz},
                },
            })

    items.sort(key=lambda x: x["sort"])
    return items


# ============================================================== Abgleich ==

def _rebase_generell(item: dict, existing: dict):
    """Serienstart des bestehenden Termins beibehalten, damit vergangene
    Wochen bei einer Aktualisierung nicht aus dem Kalender verschwinden."""
    old_start = (existing.get("start") or {}).get("dateTime")
    if not old_start:
        return
    old_date = datetime.fromisoformat(old_start).date()
    new_date = date.fromisoformat(item["body"]["start"]["dateTime"][:10])
    if old_date < new_date and old_date.weekday() == new_date.weekday():
        item["body"]["start"]["dateTime"] = datetime.combine(old_date, item["start_time"]).isoformat()
        item["body"]["end"]["dateTime"] = datetime.combine(old_date, item["end_time"]).isoformat()


def compute_plan(service, calendar_id: str, desired: list) -> dict:
    existing = {}
    marker = None
    for ev in _list_managed(service, calendar_id):
        key = ev.get("extendedProperties", {}).get("private", {}).get("bwhv_sync_key")
        if key == "marker":
            marker = ev
        elif key:
            existing[key] = ev

    plan = {"add": [], "update": [], "unchanged": [], "migrate": [], "delete": [], "old_marker": marker}

    for item in desired:
        ev = existing.pop(item["sync_key"], None)
        legacy = False
        if ev is None and item.get("legacy_prefix"):
            # Alte App-Versionen hatten die Startzeit im Schlüssel
            # ("sperrtermin-2026-09-12-0000") -> wiedererkennen statt neu anlegen.
            p = item["legacy_prefix"]
            cands = [k for k in existing if k.startswith(p) and re.fullmatch(r"\d{4}", k[len(p):])]
            if cands:
                pick = p + item["legacy_hhmm"] if p + item["legacy_hhmm"] in cands else sorted(cands)[0]
                ev, legacy = existing.pop(pick), True

        if ev is None:
            plan["add"].append(item)
            continue
        if item["category"] == "generell":
            _rebase_generell(item, ev)
        old_hash = ev.get("extendedProperties", {}).get("private", {}).get("bwhv_hash")
        if old_hash == item["hash"]:
            (plan["migrate"] if legacy else plan["unchanged"]).append((item, ev))
        else:
            item["previous"] = _event_when(ev)
            plan["update"].append((item, ev))

    plan["delete"] = sorted(existing.values(), key=lambda e: _event_when(e))
    return plan


def plan_summary(plan: dict, bis_date: date) -> dict:
    """JSON-taugliche Darstellung des Plans für die Oberfläche."""
    def it(i, extra=None):
        d = {"title": i["title"], "when": i["when"], "category": i["category"]}
        if extra:
            d.update(extra)
        return d

    return {
        "add": [it(i) for i in plan["add"]],
        "update": [it(i, {"previous": i.get("previous")}) for i, _ in plan["update"]],
        "unchanged": [it(i) for i, _ in plan["unchanged"] + plan["migrate"]],
        "delete": [{"title": e.get("summary", ""), "when": _event_when(e),
                    "category": e.get("extendedProperties", {}).get("private", {}).get("bwhv_category", "")}
                   for e in plan["delete"]],
        "marker": {"title": "FWL - aktuellster Punkt", "when": _fmt_d(bis_date),
                   "previous": _event_when(plan["old_marker"]) if plan["old_marker"] else None},
    }


def _props(item: dict) -> dict:
    return {"bwhv_managed": "1", "bwhv_category": item["category"],
            "bwhv_sync_key": item["sync_key"], "bwhv_hash": item["hash"]}


def apply_plan(service, calendar_id: str, plan: dict, bis_date: date) -> dict:
    stats = {"added": 0, "updated": 0, "unchanged": len(plan["unchanged"]) + len(plan["migrate"]),
             "deleted": 0, "errors": []}
    events = service.events()

    def run(label, fn):
        try:
            fn()
            return True
        except HttpError as e:
            stats["errors"].append(f"{label}: {e.reason if hasattr(e, 'reason') else e}")
            return False

    for item in plan["add"]:
        body = dict(item["body"], extendedProperties={"private": _props(item)})
        if run(f"Anlegen {item['title']} {item['when']}",
               lambda: events.insert(calendarId=calendar_id, body=body).execute()):
            stats["added"] += 1

    for item, ev in plan["update"]:
        body = dict(item["body"], extendedProperties={"private": _props(item)})
        if run(f"Ändern {item['title']} {item['when']}",
               lambda: events.update(calendarId=calendar_id, eventId=ev["id"], body=body).execute()):
            stats["updated"] += 1

    for item, ev in plan["migrate"]:  # nur Schlüssel auf neues Format umstellen
        run("Migration", lambda: events.patch(
            calendarId=calendar_id, eventId=ev["id"],
            body={"extendedProperties": {"private": _props(item)}}).execute())

    for ev in plan["delete"]:
        if run(f"Löschen {ev.get('summary', '')}",
               lambda: events.delete(calendarId=calendar_id, eventId=ev["id"]).execute()):
            stats["deleted"] += 1

    # Marker: immer löschen + neu anlegen
    if plan["old_marker"]:
        run("Marker löschen", lambda: events.delete(
            calendarId=calendar_id, eventId=plan["old_marker"]["id"]).execute())
    label_id = _get_label_id(service, calendar_id)
    marker = {
        "summary": "FWL - aktuellster Punkt",
        "description": _description(None, "Letzter Tag, bis zu dem die Freiwunschliste ausgefüllt ist."),
        "start": {"date": bis_date.isoformat()},
        "end": {"date": (bis_date + timedelta(days=1)).isoformat()},
        "extendedProperties": {"private": {"bwhv_managed": "1", "bwhv_category": "marker",
                                           "bwhv_sync_key": "marker", "bwhv_hash": _hash("marker", bis_date)}},
    }
    kwargs = {"calendarId": calendar_id, "body": marker}
    if label_id:
        marker["eventLabelId"] = label_id
        kwargs["eventLabelVersion"] = 1
    else:
        marker["colorId"] = MARKER_COLOR_ID_FALLBACK
        stats["marker_label_missing"] = True
    run("Marker anlegen", lambda: events.insert(**kwargs).execute())
    return stats


# ============================================================ Einstieg ====

def preview(fw, bis_date: date) -> dict:
    service = get_service()
    cal_id = get_or_create_calendar_id(service)
    plan = compute_plan(service, cal_id, build_desired(fw, bis_date))
    return plan_summary(plan, bis_date)


def sync_freiwunschliste(fw, bis_date: date) -> dict:
    service = get_service()
    cal_id = get_or_create_calendar_id(service)
    plan = compute_plan(service, cal_id, build_desired(fw, bis_date))
    stats = apply_plan(service, cal_id, plan, bis_date)
    stats["plan"] = plan_summary(plan, bis_date)
    return stats
