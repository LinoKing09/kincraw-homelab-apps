"""Google-Login (Geräte-Code-Flow) und Abgleich der Stunden mit dem Kalender.

Grundsätze (wie bei BWHV FWL Sync):
  * Nur Termine mit der privaten Kennung untis_managed=1 werden angefasst –
    eigene Einträge im Kalender bleiben immer unberührt.
  * Sync-Schlüssel = Datum + Untis-Perioden-ID (keine Uhrzeit), damit
    Zeitänderungen als Update statt als Löschen+Neu ankommen.
  * Ein Hash über den Terminkörper entscheidet, ob ein Update nötig ist.
  * Nur Termine im synchronisierten Zeitraum (heute bis +N Wochen) werden
    ggf. gelöscht – Vergangenes bleibt stehen.
"""
import hashlib
import json
import os
import time as _time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PROP = "untis_managed"
TZ = ZoneInfo(config.TIMEZONE)


class SyncError(Exception):
    pass


# ================================================================ Auth ====

def _client_info():
    if not os.path.exists(config.CLIENT_SECRET_PATH):
        raise SyncError("client_secret.json fehlt (siehe README).")
    with open(config.CLIENT_SECRET_PATH) as f:
        d = json.load(f)
    c = d.get("installed") or d.get("web") or next(iter(d.values()))
    return c["client_id"], c["client_secret"]


def _save_creds(creds):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = config.TOKEN_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, config.TOKEN_PATH)


def _load_creds():
    if not os.path.exists(config.TOKEN_PATH):
        return None
    try:
        return Credentials.from_authorized_user_file(config.TOKEN_PATH, config.SCOPES)
    except (ValueError, json.JSONDecodeError):
        return None


def is_connected() -> bool:
    c = _load_creds()
    return bool(c and (c.valid or c.refresh_token))


_pending = {}


def start_device_flow() -> dict:
    cid, _ = _client_info()
    r = requests.post(DEVICE_URL, data={"client_id": cid, "scope": " ".join(config.SCOPES)},
                      timeout=15)
    d = r.json()
    if r.status_code != 200:
        raise SyncError(f"Google: {d.get('error_description') or d.get('error')}")
    _pending.clear()
    _pending.update(device_code=d["device_code"],
                    expires=_time.time() + int(d.get("expires_in", 1800)))
    return {
        "user_code": d["user_code"],
        "verification_url": d.get("verification_url") or d.get("verification_uri"),
        "interval": int(d.get("interval", 5)),
    }


def poll_device_flow() -> dict:
    if not _pending:
        return {"status": "idle"}
    if _time.time() > _pending["expires"]:
        _pending.clear()
        return {"status": "expired"}
    cid, cs = _client_info()
    r = requests.post(TOKEN_URL, data={
        "client_id": cid, "client_secret": cs,
        "device_code": _pending["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }, timeout=15)
    d = r.json()
    if "access_token" in d:
        _save_creds(Credentials(
            token=d["access_token"], refresh_token=d.get("refresh_token"),
            token_uri=TOKEN_URL, client_id=cid, client_secret=cs, scopes=config.SCOPES,
        ))
        _pending.clear()
        return {"status": "ok"}
    if d.get("error") in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    _pending.clear()
    return {"status": "error", "message": d.get("error_description") or d.get("error")}


def disconnect():
    c = _load_creds()
    if c:
        try:
            requests.post(REVOKE_URL, params={"token": c.refresh_token or c.token}, timeout=10)
        except requests.RequestException:
            pass
    if os.path.exists(config.TOKEN_PATH):
        os.remove(config.TOKEN_PATH)


def get_service():
    creds = _load_creds()
    if not creds:
        raise SyncError("Nicht mit Google verbunden.")
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)
    # static_discovery=False: aktuelles Discovery-Dokument (nötig für eventLabelId)
    return build("calendar", "v3", credentials=creds,
                 static_discovery=False, cache_discovery=False)


# ============================================================ Calendar ====

def _get_label_id(service):
    try:
        cal = service.calendars().get(calendarId=config.CALENDAR_ID).execute(num_retries=3)
    except HttpError:
        return None
    for lbl in (cal.get("labelProperties") or {}).get("eventLabels") or []:
        if (lbl.get("name") or "").strip().lower() == config.LABEL_NAME.strip().lower():
            return lbl.get("id")
    return None


def _list_managed(service, tmin: datetime, tmax: datetime) -> list:
    items, tok = [], None
    while True:
        res = service.events().list(
            calendarId=config.CALENDAR_ID,
            privateExtendedProperty=f"{PROP}=1",
            timeMin=tmin.isoformat(), timeMax=tmax.isoformat(),
            singleEvents=True, showDeleted=False, maxResults=250, pageToken=tok,
        ).execute(num_retries=3)
        items.extend(res.get("items", []))
        tok = res.get("nextPageToken")
        if not tok:
            return items


_PREFIX = {"cancelled": "ENTFALL – ", "irregular": "Vertretung – ", "exam": "Prüfung – "}


def _event_body(lesson, label_id) -> dict:
    desc = []
    if lesson.subject_long != lesson.subject:
        desc.append(f"Fach: {lesson.subject_long} ({lesson.subject})")
    if lesson.teachers:
        desc.append(("Lehrkraft (geändert): " if lesson.teacher_changed else "Lehrkraft: ")
                    + ", ".join(lesson.teachers))
    if lesson.room_changed:
        desc.append("Raumänderung!")
    if lesson.classes:
        desc.append("Klasse: " + ", ".join(lesson.classes))
    desc += lesson.info
    desc.append("(automatisch erzeugt von Untis Sync)")

    start = datetime.combine(lesson.day, lesson.start, tzinfo=TZ)
    end = datetime.combine(lesson.day, lesson.end, tzinfo=TZ)
    body = {
        "summary": _PREFIX.get(lesson.status, "") + lesson.title,
        "location": ", ".join(lesson.rooms),
        "description": "\n".join(desc),
        "start": {"dateTime": start.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE},
        "transparency": "transparent" if lesson.status == "cancelled" else "opaque",
        "reminders": {"useDefault": False, "overrides": (
            [{"method": "popup", "minutes": int(config.REMINDER_MINUTES)}]
            if config.REMINDER_MINUTES and lesson.status != "cancelled" else [])},
    }
    if label_id:
        body["eventLabelId"] = label_id
    else:
        body["colorId"] = config.COLOR_FALLBACK

    h = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
    body["extendedProperties"] = {"private": {
        PROP: "1", "untis_key": lesson.key, "untis_hash": h}}
    return body


def _safe_delete(service, event_id):
    try:
        service.events().delete(calendarId=config.CALENDAR_ID, eventId=event_id).execute(num_retries=3)
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise


def sync(lessons: list, range_start, range_end) -> dict:
    """lessons: bereits gefilterte/zusammengefasste Liste für [range_start, range_end]."""
    service = get_service()
    label_id = _get_label_id(service)
    kw = {"eventLabelVersion": 1} if label_id else {}

    tmin = datetime.combine(range_start, dtime.min, tzinfo=TZ)
    tmax = datetime.combine(range_end + timedelta(days=1), dtime.min, tzinfo=TZ)

    by_key, dupes = {}, []
    for ev in _list_managed(service, tmin, tmax):
        k = ev.get("extendedProperties", {}).get("private", {}).get("untis_key")
        if not k or k in by_key:
            dupes.append(ev)
        else:
            by_key[k] = ev

    stats = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    for lesson in lessons:
        if lesson.status == "cancelled" and config.CANCELLED_MODE == "delete":
            continue
        body = _event_body(lesson, label_id)
        ev = by_key.pop(lesson.key, None)
        if ev is None:
            service.events().insert(calendarId=config.CALENDAR_ID, body=body, **kw).execute(num_retries=3)
            stats["added"] += 1
        elif ev.get("extendedProperties", {}).get("private", {}).get("untis_hash") == \
                body["extendedProperties"]["private"]["untis_hash"]:
            stats["unchanged"] += 1
        else:
            service.events().update(calendarId=config.CALENDAR_ID, eventId=ev["id"],
                                    body=body, **kw).execute(num_retries=3)
            stats["updated"] += 1

    for ev in list(by_key.values()) + dupes:
        _safe_delete(service, ev["id"])
        stats["deleted"] += 1

    stats["label_missing"] = label_id is None
    return stats
