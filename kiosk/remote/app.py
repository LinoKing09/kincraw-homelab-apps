"""
Kiosk-Fernbedienung – läuft als Docker-Container auf docker1.

Liefert die Bedienoberfläche aus und reicht Befehle an die API von kiosk.py
auf dem Raspberry Pi weiter. Das Token bleibt dabei auf dem Server; der Browser
im Handy sieht es nie.
"""

import os

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

KIOSK_URL = os.environ.get("KIOSK_URL", "http://kiosk-pi:8765").rstrip("/")
KIOSK_TOKEN = os.environ.get("KIOSK_TOKEN", "")
TIMEOUT = float(os.environ.get("KIOSK_TIMEOUT", "8"))

app = Flask(__name__, static_folder="static", static_url_path="/static")
session = requests.Session()
session.headers["X-Kiosk-Token"] = KIOSK_TOKEN

ALLOWED_POST = {
    "next", "prev", "goto", "rotation", "interval", "zoom", "hide_chrome",
    "display", "reload", "open", "resume_views", "views", "restart_browser",
}


def offline(e):
    return jsonify({"error": f"Kiosk nicht erreichbar ({KIOSK_URL}): {e.__class__.__name__}",
                    "offline": True}), 502


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/state")
def state():
    try:
        r = session.get(f"{KIOSK_URL}/api/state", timeout=TIMEOUT)
        return Response(r.content, r.status_code, content_type="application/json")
    except requests.RequestException as e:
        return offline(e)


@app.get("/api/screenshot")
def screenshot():
    try:
        r = session.get(f"{KIOSK_URL}/api/screenshot", timeout=TIMEOUT)
        return Response(r.content, r.status_code,
                        content_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "no-store"})
    except requests.RequestException as e:
        return offline(e)


@app.post("/api/<action>")
def command(action):
    if action not in ALLOWED_POST:
        return jsonify({"error": "unbekannter Befehl"}), 404
    try:
        r = session.post(f"{KIOSK_URL}/api/{action}",
                         json=request.get_json(silent=True) or {}, timeout=TIMEOUT)
        return Response(r.content, r.status_code, content_type="application/json")
    except requests.RequestException as e:
        return offline(e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
