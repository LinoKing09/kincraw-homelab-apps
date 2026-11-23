# -*- coding: utf-8 -*-
import hmac
import io
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from urllib.parse import quote

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from googleapiclient.errors import HttpError

import config
import gcal
import history
import notify
from parser import ParseError, parse_freiwunschliste

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
config.ensure_data_dir()
notify.start_background()

app = Flask(__name__)
app.secret_key = config.get_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=config.LOGIN_DAYS)

_import_lock = threading.Lock()

if not config.PASSWORD:
    print("WARNUNG: BWHV_PASSWORD ist nicht gesetzt – die Oberfläche ist ohne Anmeldung "
          "erreichbar. Nur im Heimnetz betreiben!", flush=True)


class UserError(ValueError):
    """Fehler mit verständlicher Meldung für die Oberfläche."""


def _api_error(msg, status=400, **extra):
    return jsonify({"error": msg, **extra}), status


def _reauth_error(msg=None):
    """401 mit reauth=True: die Oberfläche zeigt dann "Neu verbinden"."""
    return _api_error(msg or gcal.last_auth_error() or "Nicht mit Google verbunden.", 401, reauth=True)


@app.errorhandler(413)
def too_large(_):
    return _api_error("Die Datei ist zu groß (max. 10 MB).", 413)


# ------------------------------------------------------- Zugangsschutz ----

# Ohne Anmeldung erreichbar: Login, Healthcheck, PWA-Dateien und das
# Teilen-Ziel (Android schickt dort u. U. kein Cookie mit; die Datei wird nur
# zwischengelagert und erst nach der Anmeldung verarbeitet).
_PUBLIC_ENDPOINTS = {"login", "healthz", "manifest", "service_worker", "static", "share"}

_MAX_FAILS = 5
_LOCK_SECONDS = 5 * 60
_fails = {}  # ip -> (anzahl, gesperrt_bis)
_fails_lock = threading.Lock()


def _logged_in() -> bool:
    return not config.PASSWORD or session.get("logged_in") is True


def _safe_next(target: str) -> str:
    # nur relative Pfade innerhalb der App, kein "//host"
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.before_request
def require_login():
    if _logged_in() or request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if request.path.startswith("/api/") or request.path.startswith("/auth/") or request.method != "GET":
        return _api_error("Nicht angemeldet – bitte Seite neu laden.", 401)
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.PASSWORD:
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
        elif hmac.compare_digest(request.form.get("password", "").encode(), config.PASSWORD.encode()):
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


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if config.PASSWORD else url_for("index"))


# ------------------------------------------------------------- Seiten ----

@app.route("/")
def index():
    return render_template(
        "index.html",
        authenticated=gcal.is_authenticated(),
        has_client_secret=gcal.has_client_secret(),
        calendar_name=config.CALENDAR_NAME,
        label_name=config.MARKER_LABEL_NAME,
        auth_error=gcal.last_auth_error(),
        portal_url=config.PORTAL_URL,
        password_enabled=bool(config.PASSWORD),
    )


@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/service-worker.js")
def service_worker():
    resp = app.send_static_file("service-worker.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ------------------------------------------------- Teilen (Share Target) --
# Android: PDF im Portal herunterladen -> "Teilen" -> FWL Import.
# Die Datei wird unter einem Zufallsnamen abgelegt; die Oberfläche holt sie
# danach über /api/shared/<token> (nur angemeldet) ins Formular.

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


def _cleanup_shared():
    if not os.path.isdir(config.SHARED_DIR):
        return
    cutoff = time.time() - config.SHARED_MAX_AGE
    for name in os.listdir(config.SHARED_DIR):
        path = os.path.join(config.SHARED_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


@app.route("/share", methods=["POST"])
def share():
    _cleanup_shared()
    files = [f for f in request.files.getlist("pdf") if f and f.filename]
    if not files:
        return redirect(url_for("index", shared_error="Es wurde keine Datei geteilt."), 303)
    f = files[0]
    head = f.stream.read(5)
    if head != b"%PDF-":
        return redirect(url_for("index", shared_error="Die geteilte Datei ist kein PDF."), 303)
    f.stream.seek(0)
    os.makedirs(config.SHARED_DIR, exist_ok=True)
    token = secrets.token_urlsafe(24)
    f.save(os.path.join(config.SHARED_DIR, token + ".pdf"))
    # Originalnamen nur für die Anzeige merken (bereinigt)
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(f.filename))[:80] or "geteilt.pdf"
    with open(os.path.join(config.SHARED_DIR, token + ".name"), "w", encoding="utf-8") as nf:
        nf.write(name)
    return redirect(url_for("index", shared=token), 303)


@app.route("/api/shared/<token>")
def api_shared(token):
    if not _TOKEN_RE.fullmatch(token):
        return _api_error("Ungültiger Link.", 404)
    path = os.path.join(config.SHARED_DIR, token + ".pdf")
    if not os.path.isfile(path):
        return _api_error("Die geteilte Datei ist abgelaufen – bitte erneut teilen.", 404)
    name_path = os.path.join(config.SHARED_DIR, token + ".name")
    name = "geteilt.pdf"
    if os.path.isfile(name_path):
        with open(name_path, encoding="utf-8") as nf:
            name = nf.read().strip() or name
    with open(path, "rb") as pf:
        data = pf.read()
    for p in (path, name_path):  # nur einmal abrufbar
        if os.path.exists(p):
            os.remove(p)
    resp = send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=False, download_name=name)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Filename"] = quote(name)
    return resp


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# --------------------------------------------------------------- OAuth ----
# Geräte-Code-Flow: keine Redirect-URI, funktioniert auf privaten IPs.

@app.route("/auth")
def auth():
    try:
        info = gcal.start_device_flow()
    except Exception as e:
        return render_template("auth.html", error=str(e)), 400
    session["device_code"] = info["device_code"]
    return render_template(
        "auth.html",
        user_code=info["user_code"],
        verification_url=info.get("verification_url") or info.get("verification_uri"),
        interval=info.get("interval", 5),
        error=None,
    )


@app.route("/auth/poll")
def auth_poll():
    device_code = session.get("device_code")
    if not device_code:
        return jsonify({"status": "error", "error": "kein aktiver Anmeldeversuch – Seite neu laden"}), 400
    try:
        result = gcal.poll_device_flow(device_code)
    except Exception as e:
        return jsonify({"status": "pending", "warning": str(e)})
    if result.get("status") == "ok":
        session.pop("device_code", None)
    return jsonify(result)


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    gcal.disconnect()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API ----

def _suggested_bis_date() -> str:
    last = history.last()
    if last and last.get("bis_date"):
        return last["bis_date"]
    t = gcal.today()
    # Standardvorschlag beim allerersten Import: Ende des nächsten Monats
    first_next = (t.replace(day=1) + timedelta(days=32)).replace(day=1)
    return ((first_next + timedelta(days=32)).replace(day=1) - timedelta(days=1)).isoformat()


@app.route("/api/status")
def api_status():
    return jsonify({
        "authenticated": gcal.is_authenticated(),
        "has_client_secret": gcal.has_client_secret(),
        "calendar_name": config.CALENDAR_NAME,
        "today": gcal.today().isoformat(),
        "suggested_bis_date": _suggested_bis_date(),
        "last_import": history.last(),
        "notify": notify.public_settings(),
    })


def _read_request():
    """PDF + Datum aus dem Formular lesen und parsen. -> (fw, bis_date, filename)"""
    file = request.files.get("pdf")
    if not file or not file.filename:
        raise UserError("Keine Datei ausgewählt.")
    bis = request.form.get("bis_date", "")
    try:
        bis_date = date.fromisoformat(bis)
    except ValueError:
        raise UserError("Bitte gib an, bis zu welchem Datum die Liste ausgefüllt ist.")

    # Der Originalname spielt keine Rolle (z. B. "tkJ79_.pdf") – gespeichert
    # wird immer unter einem eigenen temporären Namen.
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        file.save(tmp_path)
        with open(tmp_path, "rb") as f:
            if f.read(5) != b"%PDF-":
                raise UserError("Die Datei ist kein PDF.")
        fw = parse_freiwunschliste(tmp_path)
    except ParseError as e:
        raise UserError(str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if fw.is_empty:
        raise UserError("In der PDF wurden keine Sperrzeiten gefunden. "
                        "Ist es die Freiwunschliste im gewohnten Format?")
    return fw, bis_date, file.filename


def _parsed_summary(fw):
    return {
        "last_change": fw.last_change.strftime("%d.%m.%Y %H:%M") if fw.last_change else None,
        "counts": {
            "generelle_sperrzeiten": len(fw.generelle_sperrzeiten),
            "sperrzeitraeume": len(fw.sperrzeitraeume),
            "weitere_sperrtermine": len(fw.weitere_sperrtermine),
        },
    }


def _newest_imported_change():
    """Jüngster PDF-Stand ("letzte Änderung") aller bisherigen Importe."""
    newest = None
    for h in history.load():
        try:
            dt = datetime.strptime(h.get("last_change") or "", "%d.%m.%Y %H:%M")
        except ValueError:
            continue
        newest = dt if newest is None or dt > newest else newest
    return newest


def _older_pdf(fw):
    """Info, falls die PDF älter ist als eine schon importierte, sonst None."""
    newest = _newest_imported_change()
    if fw.last_change and newest and fw.last_change < newest:
        return {"pdf": fw.last_change.strftime("%d.%m.%Y %H:%M"),
                "newest": newest.strftime("%d.%m.%Y %H:%M")}
    return None


def _google_error(e: HttpError):
    status = getattr(e.resp, "status", "?")
    if status == 401:
        # Token wurde akzeptiert, der Zugriff aber inzwischen entzogen
        gcal.invalidate_token()
        return _reauth_error()
    if status == 403:
        return _api_error(f"Google hat den Zugriff verweigert ({status}): {getattr(e, 'reason', e)}", 502)
    return _api_error(f"Fehler von Google Kalender ({status}): {getattr(e, 'reason', e)}", 502)


@app.route("/api/preview", methods=["POST"])
def api_preview():
    if not gcal.is_authenticated():
        return _reauth_error()
    try:
        fw, bis_date, _ = _read_request()
        plan = gcal.preview(fw, bis_date)
        return jsonify({"ok": True, **_parsed_summary(fw), "plan": plan, "older_pdf": _older_pdf(fw)})
    except UserError as e:
        return _api_error(str(e), 422)
    except gcal.ReauthRequired as e:
        return _reauth_error(str(e))
    except HttpError as e:
        return _google_error(e)
    except Exception as e:
        traceback.print_exc()
        return _api_error(f"Unerwarteter Fehler: {e}", 500)


@app.route("/api/import", methods=["POST"])
@app.route("/upload", methods=["POST"])  # Kompatibilität mit älterer Oberfläche
def api_import():
    if not gcal.is_authenticated():
        return _reauth_error()
    if not _import_lock.acquire(blocking=False):
        return _api_error("Es läuft bereits ein Import – bitte kurz warten.", 409)
    try:
        fw, bis_date, filename = _read_request()
        older = _older_pdf(fw)
        if older and request.form.get("force") != "1":
            return _api_error(
                f"Diese PDF hat den Stand {older['pdf']}, du hast aber schon einen neueren "
                f"Stand ({older['newest']}) importiert. Ein Import könnte neuere Sperrtermine löschen.",
                409, older_pdf=older)
        stats = gcal.sync_freiwunschliste(fw, bis_date)
        summary = _parsed_summary(fw)
        history.add({
            "filename": filename,
            "last_change": summary["last_change"],
            "bis_date": bis_date.isoformat(),
            "added": stats["added"], "updated": stats["updated"],
            "unchanged": stats["unchanged"], "deleted": stats["deleted"],
            "errors": len(stats["errors"]),
        })
        return jsonify({
            "ok": True, **summary,
            "sync": {
                "hinzugefuegt": stats["added"], "aktualisiert": stats["updated"],
                "unveraendert": stats["unchanged"], "geloescht": stats["deleted"],
            },
            "plan": stats["plan"],
            "errors": stats["errors"],
            "marker_label_missing": stats.get("marker_label_missing", False),
        })
    except UserError as e:
        return _api_error(str(e), 422)
    except gcal.ReauthRequired as e:
        return _reauth_error(str(e))
    except HttpError as e:
        return _google_error(e)
    except Exception as e:
        traceback.print_exc()
        return _api_error(f"Unerwarteter Fehler: {e}", 500)
    finally:
        _import_lock.release()


@app.route("/api/events")
def api_events():
    if not gcal.is_authenticated():
        return _reauth_error()
    try:
        return jsonify(gcal.list_upcoming())
    except gcal.ReauthRequired as e:
        return _reauth_error(str(e))
    except HttpError as e:
        return _google_error(e)


@app.route("/api/notify", methods=["GET", "POST"])
def api_notify():
    if request.method == "GET":
        return jsonify(notify.public_settings())
    try:
        return jsonify({"ok": True, **notify.save_settings(request.get_json(silent=True) or {})})
    except notify.NotifyError as e:
        return _api_error(str(e), 422)


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    try:
        notify.send("BWHV FWL Sync", "Test: Erinnerungen kommen an. ✓", priority=4)
    except notify.NotifyError as e:
        return _api_error(str(e), 422)
    return jsonify({"ok": True})


@app.route("/api/history")
def api_history():
    return jsonify({"history": history.load()})


if __name__ == "__main__":
    # Nur für lokale Tests – im Container läuft gunicorn (siehe Dockerfile).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
