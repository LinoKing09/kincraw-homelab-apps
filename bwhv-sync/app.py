# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import traceback
from datetime import date, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from googleapiclient.errors import HttpError

import config
import gcal
import history
from parser import ParseError, parse_freiwunschliste

config.ensure_data_dir()

app = Flask(__name__)
app.secret_key = config.get_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

_import_lock = threading.Lock()


class UserError(ValueError):
    """Fehler mit verständlicher Meldung für die Oberfläche."""


def _api_error(msg, status=400):
    return jsonify({"error": msg}), status


@app.errorhandler(413)
def too_large(_):
    return _api_error("Die Datei ist zu groß (max. 10 MB).", 413)


# ------------------------------------------------------------- Seiten ----

@app.route("/")
def index():
    return render_template(
        "index.html",
        authenticated=gcal.is_authenticated(),
        has_client_secret=gcal.has_client_secret(),
        calendar_name=config.CALENDAR_NAME,
        label_name=config.MARKER_LABEL_NAME,
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


def _google_error(e: HttpError) -> str:
    status = getattr(e.resp, "status", "?")
    if status in (401, 403):
        return f"Google hat den Zugriff verweigert ({status}). Ggf. unten 'Verbindung trennen' und neu verbinden."
    return f"Fehler von Google Kalender ({status}): {getattr(e, 'reason', e)}"


@app.route("/api/preview", methods=["POST"])
def api_preview():
    if not gcal.is_authenticated():
        return _api_error("Nicht mit Google verbunden.", 401)
    try:
        fw, bis_date, _ = _read_request()
        plan = gcal.preview(fw, bis_date)
        return jsonify({"ok": True, **_parsed_summary(fw), "plan": plan})
    except UserError as e:
        return _api_error(str(e), 422)
    except HttpError as e:
        return _api_error(_google_error(e), 502)
    except Exception as e:
        traceback.print_exc()
        return _api_error(f"Unerwarteter Fehler: {e}", 500)


@app.route("/api/import", methods=["POST"])
@app.route("/upload", methods=["POST"])  # Kompatibilität mit älterer Oberfläche
def api_import():
    if not gcal.is_authenticated():
        return _api_error("Nicht mit Google verbunden.", 401)
    if not _import_lock.acquire(blocking=False):
        return _api_error("Es läuft bereits ein Import – bitte kurz warten.", 409)
    try:
        fw, bis_date, filename = _read_request()
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
    except HttpError as e:
        return _api_error(_google_error(e), 502)
    except Exception as e:
        traceback.print_exc()
        return _api_error(f"Unerwarteter Fehler: {e}", 500)
    finally:
        _import_lock.release()


@app.route("/api/events")
def api_events():
    if not gcal.is_authenticated():
        return _api_error("Nicht mit Google verbunden.", 401)
    try:
        return jsonify({"events": gcal.list_upcoming()})
    except HttpError as e:
        return _api_error(_google_error(e), 502)


@app.route("/api/history")
def api_history():
    return jsonify({"history": history.load()})


if __name__ == "__main__":
    # Nur für lokale Tests – im Container läuft gunicorn (siehe Dockerfile).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
