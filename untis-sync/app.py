import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory
from googleapiclient.errors import HttpError

import config
import excl
import gcal
from untis import Lesson, UntisClient, UntisError, normalize, merge_double_lessons

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("untis-sync")

app = Flask(__name__, static_folder="static")
_sync_lock = threading.Lock()      # nur ein Sync gleichzeitig
_state_lock = threading.Lock()
_queue_lock = threading.Lock()
_queued = {"v": False}             # ein nachgeholter Sync wartet bereits
_next_auto = {"at": None}


# ============================================================== State =====

def _load_state() -> dict:
    try:
        with open(config.STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last": None, "history": [], "lessons": []}


def _save_state(state: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = config.STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, config.STATE_PATH)


def _now():
    return datetime.now(gcal.TZ)


# =============================================================== Sync =====

def _fetch_lessons(week_start, end) -> list:
    with UntisClient() as u:
        raw = u.timetable(week_start, end)
    lessons = normalize(raw)
    if config.MERGE_DOUBLE:
        lessons = merge_double_lessons(lessons, config.MERGE_GAP_MIN)
    return lessons


def _do_sync(trigger: str, fetch: bool) -> dict:
    """Erwartet gehaltenen _sync_lock und gibt ihn am Ende frei.
    fetch=False: nur Kalender mit den zuletzt geladenen Stunden abgleichen
    (z. B. nach dem Aus-/Einblenden) – ohne erneute WebUntis-Abfrage."""
    t0 = time.time()
    result = {"ok": False, "trigger": trigger, "at": _now().isoformat(timespec="seconds")}
    all_lessons = None
    try:
        today = _now().date()
        week_start = today - timedelta(days=today.weekday())   # Montag dieser Woche
        end = today + timedelta(weeks=config.WEEKS_AHEAD)

        cached = _load_state().get("lessons") or []
        if fetch or not cached:
            all_lessons = _fetch_lessons(week_start, end)
        else:
            all_lessons = [Lesson.from_dict(d) for d in cached]

        excl.prune(today.isoformat())
        ex = excl.load()
        to_sync, skipped = [], 0
        for l in all_lessons:
            if l.day < today:
                continue
            if excl.reason(ex, l.key, l.day.isoformat(), l.rule_id):
                skipped += 1
            else:
                to_sync.append(l)

        stats = gcal.sync(to_sync, today, end)
        stats["excluded"] = skipped
        result.update(ok=True, stats=stats, lessons=len(to_sync),
                      range=f"{today:%d.%m.} – {end:%d.%m.%Y}")
        log.info("Sync (%s) ok: %s", trigger, stats)
    except (UntisError, gcal.SyncError) as e:
        result["error"] = str(e)
        log.warning("Sync (%s) fehlgeschlagen: %s", trigger, e)
    except HttpError as e:
        result["error"] = f"Google-Kalender-Fehler: {getattr(e, 'reason', e)}"
        log.warning("Sync (%s) Google-Fehler: %s", trigger, e)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"Unerwarteter Fehler: {e}"
        log.exception("Sync (%s) unerwarteter Fehler", trigger)
    finally:
        result["duration_s"] = round(time.time() - t0, 1)
        with _state_lock:
            st = _load_state()
            st["last"] = result
            st["history"] = ([result] + st.get("history", []))[:20]
            if all_lessons is not None:
                st["lessons"] = [l.to_dict() for l in all_lessons]
            st.pop("upcoming", None)
            _save_state(st)
        _sync_lock.release()
    return result


def run_sync(trigger: str) -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "Es läuft bereits ein Sync."}
    return _do_sync(trigger, fetch=True)


def request_sync(trigger: str, fetch: bool = False):
    """Sync im Hintergrund anstoßen; wartet ggf. auf einen laufenden Sync.
    Mehrere schnelle Klicks ergeben nur einen nachgeholten Sync."""
    with _queue_lock:
        if _queued["v"]:
            return
        _queued["v"] = True

    def bg():
        _sync_lock.acquire()
        with _queue_lock:
            _queued["v"] = False
        _do_sync(trigger, fetch)

    threading.Thread(target=bg, daemon=True).start()


def _scheduler():
    time.sleep(20)
    while True:
        if gcal.is_connected() and config.untis_configured():
            run_sync("auto")
        _next_auto["at"] = (_now() + timedelta(minutes=config.SYNC_INTERVAL_MIN)).isoformat(timespec="seconds")
        time.sleep(config.SYNC_INTERVAL_MIN * 60)


if os.environ.get("DISABLE_SCHEDULER") != "1" and config.SYNC_INTERVAL_MIN > 0:
    threading.Thread(target=_scheduler, daemon=True, name="scheduler").start()


# ============================================================= Routes =====

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/status")
def status():
    st = _load_state()
    ex = excl.load()
    today = _now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    lessons = []
    for d in st.get("lessons", []):
        if d["day"] < week_start:
            continue
        d = dict(d)
        d["excluded"] = excl.reason(ex, d["key"], d["day"], d["rule_id"])
        d["rule_active"] = d["rule_id"] in {r["id"] for r in ex["rules"]}
        d["past"] = d["day"] < today.isoformat()
        lessons.append(d)
    return jsonify({
        "google_connected": gcal.is_connected(),
        "untis_configured": config.untis_configured(),
        "untis_school": config.UNTIS_SCHOOL,
        "calendar": config.CALENDAR_ID,
        "label": config.LABEL_NAME,
        "interval_min": config.SYNC_INTERVAL_MIN,
        "weeks": config.WEEKS_AHEAD,
        "next_auto": _next_auto["at"],
        "running": _sync_lock.locked() or _queued["v"],
        "today": today.isoformat(),
        "last": st.get("last"),
        "history": st.get("history", []),
        "lessons": lessons,
        "excluded_days": ex["days"],
        "rules": ex["rules"],
    })


@app.post("/api/sync")
def sync_now():
    res = run_sync("manuell")
    return jsonify(res), (200 if res.get("ok") else 409 if "bereits" in res.get("error", "") else 500)


@app.post("/api/exclusions")
def exclusions():
    p = request.get_json(silent=True) or {}
    try:
        excl.apply(p.get("action", ""), p)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    syncing = gcal.is_connected() and config.untis_configured()
    if syncing:
        request_sync("Auswahl geändert", fetch=False)
    return jsonify({"ok": True, "syncing": syncing})


@app.post("/api/auth/start")
def auth_start():
    try:
        return jsonify(gcal.start_device_flow())
    except gcal.SyncError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/auth/poll")
def auth_poll():
    try:
        return jsonify(gcal.poll_device_flow())
    except gcal.SyncError as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.post("/api/auth/disconnect")
def auth_disconnect():
    gcal.disconnect()
    return jsonify({"ok": True})
