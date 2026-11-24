import hmac
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from googleapiclient.errors import HttpError

import config
import excl
import gcal
import names
import project
import settings
from untis import Lesson, UntisClient, UntisError, normalize, normalize_rest, merge_double_lessons

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("untis-sync")

app = Flask(__name__, static_folder="static")
app.secret_key = config.get_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=config.LOGIN_DAYS)
if not config.APP_PASSWORD:
    log.warning("APP_PASSWORD ist nicht gesetzt – die Oberfläche ist ohne Anmeldung "
                "erreichbar. Nur im Heimnetz betreiben!")
_sync_lock = threading.Lock()      # nur ein Sync gleichzeitig
_state_lock = threading.Lock()
_queue_lock = threading.Lock()
_queued = {"v": False, "fetch": False}   # ein nachgeholter Sync wartet bereits
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

def _fetch_lessons(week_start, end):
    """Echte Stunden aus WebUntis + Ferienliste (None, wenn nicht abrufbar)
    + alle Stunden der neuen Schnittstelle (für deren Fachnamen-Schreibweise).

    Hauptquelle ist getTimetable. Tage danach, die nur die neue Schnittstelle
    (Web-Oberfläche/App) schon zeigt, werden von dort ergänzt. Fällt die neue
    Schnittstelle aus, läuft der Sync ohne sie weiter."""
    rest_all = []
    with UntisClient() as u:
        lessons = normalize(u.timetable(week_start, end))
        holidays = u.holidays()
        rest_from = (max(l.day for l in lessons) + timedelta(days=1)) if lessons else week_start
        if config.USE_REST:
            try:
                rest_all = normalize_rest(u.rest_entries(week_start, end))
                extra = [l for l in rest_all if rest_from <= l.day <= end]
                if extra:
                    log.info("Neue Schnittstelle: %d Stunden bis %s ergänzt",
                             len(extra), max(l.day for l in extra).isoformat())
                lessons += extra
            except UntisError as e:
                log.warning("%s – weiter ohne", e)
    lessons.sort(key=lambda l: (l.day, l.start, l.subject))
    if config.MERGE_DOUBLE:
        lessons = merge_double_lessons(lessons, config.MERGE_GAP_MIN)
    return lessons, holidays, rest_all


def _do_sync(trigger: str, fetch: bool) -> dict:
    """Erwartet gehaltenen _sync_lock und gibt ihn am Ende frei.
    fetch=False: nur Kalender mit den zuletzt geladenen Stunden abgleichen
    (z. B. nach dem Aus-/Einblenden) – ohne erneute WebUntis-Abfrage."""
    t0 = time.time()
    result = {"ok": False, "trigger": trigger, "at": _now().isoformat(timespec="seconds")}
    all_lessons = None
    extra = {}                      # zusätzliche Schlüssel für state.json
    try:
        cfg = settings.load()
        today = _now().date()
        week_start = today - timedelta(days=today.weekday())   # Montag dieser Woche
        end = today + timedelta(weeks=cfg["weeks"])

        st0 = _load_state()
        cached = st0.get("lessons") or []
        if fetch or not cached:
            real, holidays, rest_all = _fetch_lessons(week_start, end)
            extra["subject_names"] = names.collect(real + rest_all, st0.get("subject_names") or {})
            extra["untis_until"] = max(l.day for l in real).isoformat() if real else None
            extra["holidays_known"] = holidays is not None
            if cfg["fill_missing"]:
                all_lessons, template, n = project.fill_missing(
                    real, week_start, end, holidays, st0.get("template"))
                if template:
                    extra["template"] = template
                if n:
                    log.info("%d vorläufige Stunden aus Vorlagewoche %s ergänzt",
                             n, template["source_week"])
            else:
                all_lessons = real
        else:
            all_lessons = [Lesson.from_dict(d) for d in cached if d["day"] <= end.isoformat()]
        names.apply(all_lessons, cfg["names"], extra.get("subject_names") or st0.get("subject_names") or {})

        excl.prune(today.isoformat())
        ex = excl.load()
        to_sync, skipped = [], 0
        for l in all_lessons:
            if l.day < today:
                continue
            if excl.reason(ex, l.key, l.day.isoformat(), l.rule_id, l.alt_key):
                skipped += 1
            else:
                to_sync.append(l)

        # Wurde der Zeitraum verkürzt, die Termine dahinter wieder entfernen
        prev_until = st0.get("synced_until")
        stats = gcal.sync(to_sync, today, end,
                          cleanup_end=date.fromisoformat(prev_until) if prev_until else None)
        extra["synced_until"] = end.isoformat()
        stats["excluded"] = skipped
        stats["projected"] = sum(1 for l in to_sync if l.projected)
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
            st.update(extra)
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
        _queued["fetch"] = _queued["fetch"] or fetch
        if _queued["v"]:
            return
        _queued["v"] = True

    def bg():
        _sync_lock.acquire()
        with _queue_lock:
            do_fetch = _queued["fetch"]
            _queued.update(v=False, fetch=False)
        _do_sync(trigger, do_fetch)

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


# ======================================================= Zugangsschutz =====

# Ohne Anmeldung erreichbar: Login, Healthcheck und PWA-Dateien
_PUBLIC_ENDPOINTS = {"login", "healthz", "manifest", "static"}
_MAX_FAILS = 5
_LOCK_SECONDS = 5 * 60
_fails = {}                     # ip -> (anzahl, gesperrt_bis)
_fails_lock = threading.Lock()


def _logged_in() -> bool:
    return not config.APP_PASSWORD or session.get("logged_in") is True


def _safe_next(target: str) -> str:
    # nur relative Pfade innerhalb der App, kein "//host"
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.before_request
def require_login():
    if _logged_in() or request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/api/") or request.method != "GET":
        return jsonify({"error": "Nicht angemeldet – bitte Seite neu laden."}), 401
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.APP_PASSWORD:
        return redirect(url_for("index"))
    nxt = _safe_next(request.values.get("next", ""))
    ip = request.remote_addr or "?"
    error = None
    if request.method == "POST":
        now = time.time()
        with _fails_lock:
            count, locked_until = _fails.get(ip, (0, 0))
        if locked_until > now:
            error = f"Zu viele Fehlversuche – bitte in {int((locked_until - now) // 60) + 1} Minuten erneut versuchen."
        elif hmac.compare_digest(request.form.get("password", "").encode(), config.APP_PASSWORD.encode()):
            with _fails_lock:
                _fails.pop(ip, None)
            session.clear()
            session.permanent = True
            session["logged_in"] = True
            return redirect(nxt)
        else:
            count += 1
            with _fails_lock:
                _fails[ip] = (0, now + _LOCK_SECONDS) if count >= _MAX_FAILS else (count, 0)
            error = "Falsches Passwort."
    elif _logged_in():
        return redirect(nxt)
    return render_template("login.html", error=error, next=nxt), (401 if error else 200)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if config.APP_PASSWORD else url_for("index"))


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
    cfg = settings.load()
    today = _now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    lessons = []
    for d in st.get("lessons", []):
        if d["day"] < week_start:
            continue
        d = dict(d)
        d["excluded"] = excl.reason(ex, d["key"], d["day"], d["rule_id"], d.get("alt_key"))
        d.setdefault("projected", False)
        d["rule_active"] = d["rule_id"] in {r["id"] for r in ex["rules"]}
        d["past"] = d["day"] < today.isoformat()
        lessons.append(d)
    return jsonify({
        "password_enabled": bool(config.APP_PASSWORD),
        "google_connected": gcal.is_connected(),
        "untis_configured": config.untis_configured(),
        "untis_school": config.UNTIS_SCHOOL,
        "calendar": config.CALENDAR_ID,
        "label": config.LABEL_NAME,
        "interval_min": config.SYNC_INTERVAL_MIN,
        "weeks": cfg["weeks"],
        "weeks_min": settings.WEEKS_MIN,
        "weeks_max": settings.WEEKS_MAX,
        "fill_missing": cfg["fill_missing"],
        "names": cfg["names"],
        "name_examples": names.examples(st.get("subject_names") or {}),
        "untis_until": st.get("untis_until"),
        "holidays_known": st.get("holidays_known"),
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


@app.post("/api/settings")
def save_settings():
    p = request.get_json(silent=True) or {}
    try:
        cfg = settings.update({k: p[k] for k in ("weeks", "fill_missing", "names") if k in p})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    syncing = gcal.is_connected() and config.untis_configured()
    if syncing:
        request_sync("Einstellungen geändert", fetch=True)
    return jsonify({"ok": True, "syncing": syncing, **cfg})


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
