#!/usr/bin/env python3
"""
kiosk.py – Home-Assistant-Kiosk für den Raspberry Pi.

Ein einziger Prozess, der
  * Chromium im Kiosk-Modus startet (und neu startet, falls er abstürzt),
  * die konfigurierten HA-Ansichten per Chrome DevTools Protocol (CDP)
    nacheinander anzeigt – ohne Tabs, ohne Flackern,
  * das Display per `wlopm` ein-/ausschaltet (optional per PIR-Sensor),
  * eine kleine, per Token geschützte REST-API auf Port 8765 anbietet,
    über die die Fernbedienung (Docker-Container) alles steuert.

Abhängigkeiten (Raspberry Pi OS):  sudo apt install python3-websocket wlopm
Optional für PIR:                   python3-gpiozero (meist vorinstalliert)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import websocket  # python3-websocket / websocket-client

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

BASE_DIR = Path(os.environ.get("KIOSK_DIR", Path(__file__).resolve().parent))
CONFIG_FILE = BASE_DIR / "config.json"
CDP_PORT = 9222

DEFAULT_CONFIG = {
    # Wird beim ersten Start zufällig erzeugt – in die .env der Fernbedienung kopieren
    "token": "",
    "api_port": 8765,
    "views": [
        {"name": "Übersicht", "url": "http://192.168.179.73:8123/dashboard-familyview/view-1"},
        {"name": "Ansicht 2", "url": "http://192.168.179.73:8123/dashboard-familyview/view-2"},
        {"name": "Ansicht 3", "url": "http://192.168.179.73:8123/dashboard-familyview/view-3"},
    ],
    "interval": 30,            # Sekunden pro Ansicht (Standard)
    "rotating": True,
    "zoom": 1.0,
    "hide_ha_chrome": True,    # Kopfzeile/Seitenleiste von HA ausblenden
    "display_output": "*",     # wlopm-Ausgang, z. B. "HDMI-A-2"; "*" = alle
    "pir_pin": None,           # GPIO-Nummer des PIR-Sensors, z. B. 17; None = aus
    "pir_timeout": 300,        # Sekunden ohne Bewegung bis Display aus
    "full_reload_hours": 6,    # Seite regelmäßig komplett neu laden (gegen Speicherlecks)
    "chromium_flags": [],      # zusätzliche Flags
    "user_data_dir": str(Path.home() / ".chromium-kiosk"),
}

log = logging.getLogger("kiosk")


class Config:
    """Thread-sichere, auf Platte gespeicherte Konfiguration."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        data = dict(DEFAULT_CONFIG)
        if path.exists():
            try:
                data.update(json.loads(path.read_text("utf-8")))
            except Exception as e:  # kaputte Datei nicht überschreiben
                raise SystemExit(f"config.json ist ungültig: {e}")
        if not data.get("token"):
            data["token"] = secrets.token_urlsafe(24)
            log.warning("Neues API-Token erzeugt: %s", data["token"])
        self.data = data
        self.save()

    def __getitem__(self, key):
        with self.lock:
            return self.data[key]

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.save()

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)


# --------------------------------------------------------------------------
# Chrome DevTools Protocol
# --------------------------------------------------------------------------


class CDPError(Exception):
    pass


class CDP:
    """Minimaler synchroner CDP-Client für genau einen Seiten-Tab."""

    def __init__(self, port: int):
        self.port = port
        self.ws: websocket.WebSocket | None = None
        self.lock = threading.Lock()
        self.msg_id = 0
        self.zoom_script_id: str | None = None

    def _targets(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as r:
            return json.load(r)

    def alive(self) -> bool:
        try:
            self._targets()
            return True
        except Exception:
            return False

    def connect(self):
        pages = [t for t in self._targets() if t.get("type") == "page"]
        if not pages:
            # Kein Tab offen (sollte im Kiosk-Modus nicht passieren) → einen anlegen
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
            with urllib.request.urlopen(req, timeout=3) as r:
                pages = [json.load(r)]
        self.ws = websocket.create_connection(
            pages[0]["webSocketDebuggerUrl"], timeout=10, suppress_origin=True
        )
        self.zoom_script_id = None
        log.info("CDP verbunden")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        finally:
            self.ws = None

    def call(self, method: str, params: dict | None = None, timeout: float = 10):
        with self.lock:
            if self.ws is None:
                self.connect()
            self.msg_id += 1
            mid = self.msg_id
            try:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    self.ws.settimeout(max(0.1, deadline - time.time()))
                    msg = json.loads(self.ws.recv())
                    if msg.get("id") == mid:  # Events (ohne id) werden ignoriert
                        if "error" in msg:
                            raise CDPError(msg["error"].get("message", str(msg["error"])))
                        return msg.get("result", {})
                raise CDPError(f"Timeout bei {method}")
            except (websocket.WebSocketException, OSError, ConnectionError) as e:
                self.close()
                raise CDPError(f"Verbindung verloren: {e}") from e

    def eval(self, expression: str):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return res.get("result", {}).get("value")


# Skript, das bei jedem Seitenaufbau Zoom und ggf. HA-Kopfzeile setzt.
PAGE_SETUP_JS = r"""
(() => {
  const ZOOM = %(zoom)s, HIDE = %(hide)s;
  const apply = () => {
    if (document.documentElement) document.documentElement.style.zoom = ZOOM;
    if (!HIDE) return;
    // HA versteckt Kopfzeile/Seitenleiste mit diesen Query-Parametern nicht nativ,
    // daher per CSS in die Shadow-DOMs.
    try {
      const ha = document.querySelector('home-assistant');
      const main = ha?.shadowRoot?.querySelector('home-assistant-main');
      const drawer = main?.shadowRoot?.querySelector('ha-drawer');
      if (drawer) drawer.style.setProperty('--mdc-drawer-width', '0px');
      const sidebar = main?.shadowRoot?.querySelector('ha-sidebar');
      if (sidebar) sidebar.style.display = 'none';
      const panel = main?.shadowRoot?.querySelector('ha-panel-lovelace');
      const root = panel?.shadowRoot?.querySelector('hui-root');
      const header = root?.shadowRoot?.querySelector('.header');
      if (header) header.style.display = 'none';
      const view = root?.shadowRoot?.querySelector('#view');
      if (view) { view.style.paddingTop = '0'; view.style.marginTop = '0'; }
    } catch (e) {}
  };
  apply();
  document.addEventListener('DOMContentLoaded', apply);
  window.addEventListener('location-changed', () => setTimeout(apply, 50));
  setInterval(apply, 2000);   // HA rendert Teile nachträglich
})();
"""


# --------------------------------------------------------------------------
# Kiosk-Logik
# --------------------------------------------------------------------------


class Kiosk:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cdp = CDP(CDP_PORT)
        self.chromium: subprocess.Popen | None = None
        self.index = 0
        self.override_url: str | None = None    # temporär angezeigte URL
        self.override_until = 0.0
        self.next_switch = time.time()
        self.display_on = True
        self.last_motion = time.time()
        self.last_full_reload = time.time()
        self.current_url = ""
        self.last_error = ""
        self.wake = threading.Condition()
        self.stopping = False

    # ---------- Chromium ----------

    def chromium_binary(self) -> str:
        for name in ("chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
        raise SystemExit("Chromium nicht gefunden (sudo apt install chromium)")

    def start_chromium(self):
        views = self.cfg["views"]
        start_url = views[self.index]["url"] if views else "about:blank"
        args = [
            os.environ.get("KIOSK_CHROMIUM") or self.chromium_binary(),
            "--kiosk",
            f"--remote-debugging-port={CDP_PORT}",   # nur auf 127.0.0.1 erreichbar
            f"--user-data-dir={self.cfg['user_data_dir']}",
            "--noerrdialogs",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--disable-features=Translate,MediaRouter",
            "--no-first-run",
            "--password-store=basic",
            "--check-for-update-interval=31536000",
            "--ozone-platform=wayland",
            *self.cfg["chromium_flags"],
            start_url,
        ]
        if os.environ.get("KIOSK_HEADLESS"):           # nur für Tests ohne Bildschirm
            args = [a for a in args if a not in ("--kiosk", "--ozone-platform=wayland")]
            args.insert(1, "--headless=new")
            args.insert(2, "--window-size=1280,800")
        log.info("Starte Chromium")
        self.chromium = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        for _ in range(60):
            if self.cdp.alive():
                break
            if self.chromium.poll() is not None:
                raise CDPError(f"Chromium ist sofort beendet worden (Code {self.chromium.returncode}) – "
                               "Flags prüfen oder Chromium manuell im Terminal starten")
            time.sleep(0.5)
        else:
            raise CDPError("Chromium antwortet nicht auf Port 9222")
        self.cdp.close()
        self.setup_page()
        self.current_url = start_url
        self.override_url = None
        self.last_full_reload = time.time()
        if views:
            self.next_switch = time.time() + self.view_seconds(views[self.index])

    def ensure_chromium(self):
        if self.chromium and self.chromium.poll() is not None:
            log.warning("Chromium wurde beendet (Code %s)", self.chromium.returncode)
            self.chromium = None
        if not self.cdp.alive():
            self.cdp.close()
            self.start_chromium()

    def setup_page(self):
        """Zoom/CSS-Skript registrieren (gilt auch für jeden späteren Seitenaufbau)."""
        js = PAGE_SETUP_JS % {
            "zoom": float(self.cfg["zoom"]),
            "hide": "true" if self.cfg["hide_ha_chrome"] else "false",
        }
        self.cdp.call("Page.enable")
        if self.cdp.zoom_script_id:
            try:
                self.cdp.call("Page.removeScriptToEvaluateOnNewDocument",
                              {"identifier": self.cdp.zoom_script_id})
            except CDPError:
                pass
        res = self.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": js})
        self.cdp.zoom_script_id = res.get("identifier")
        self.cdp.eval(js)  # sofort auf die aktuelle Seite anwenden

    # ---------- Navigation ----------

    def navigate(self, url: str, full_reload: bool = False):
        """Gleicher Ursprung → HA-interne Navigation ohne Neuladen, sonst Page.navigate."""
        self.ensure_chromium()
        if self.cdp.zoom_script_id is None:
            self.setup_page()
        target = urlparse(url)
        try:
            cur = urlparse(self.cdp.eval("location.href") or "")
        except CDPError:
            cur = urlparse("")
        same_origin = (cur.scheme, cur.netloc) == (target.scheme, target.netloc)
        is_ha = bool(self.cdp.eval("!!document.querySelector('home-assistant')")) if same_origin else False

        if same_origin and is_ha and not full_reload:
            path = json.dumps(target.path + (f"?{target.query}" if target.query else ""))
            self.cdp.eval(
                f"history.pushState(null, '', {path});"
                "window.dispatchEvent(new CustomEvent('location-changed', {detail: {replace: false}}));"
            )
        else:
            self.cdp.call("Page.navigate", {"url": url})
            self.last_full_reload = time.time()
        self.current_url = url
        self.last_error = ""
        log.info("→ %s", url)

    def current_view(self):
        views = self.cfg["views"]
        if not views:
            return None
        self.index %= len(views)
        return views[self.index]

    def view_seconds(self, view) -> int:
        return int(view.get("seconds") or self.cfg["interval"])

    def show_index(self, i: int, full_reload: bool = False):
        views = self.cfg["views"]
        if not views:
            return
        self.index = i % len(views)
        self.override_url = None
        view = views[self.index]
        self.navigate(view["url"], full_reload)
        self.next_switch = time.time() + self.view_seconds(view)

    def poke(self):
        with self.wake:
            self.wake.notify_all()

    # ---------- Display ----------

    def set_display(self, on: bool, reason: str = ""):
        if on == self.display_on:
            return
        out = self.cfg["display_output"]
        try:
            subprocess.run(["wlopm", "--on" if on else "--off", out],
                           check=True, timeout=5, capture_output=True)
            self.display_on = on
            log.info("Display %s %s", "an" if on else "aus", f"({reason})" if reason else "")
        except FileNotFoundError:
            self.last_error = "wlopm ist nicht installiert"
            self.display_on = on  # Zustand trotzdem merken (Tests)
        except subprocess.CalledProcessError as e:
            self.last_error = f"wlopm: {e.stderr.decode(errors='replace').strip()}"
            log.error(self.last_error)

    def motion(self):
        self.last_motion = time.time()
        if not self.display_on:
            self.set_display(True, "Bewegung")

    def start_pir(self):
        pin = self.cfg["pir_pin"]
        if pin is None:
            return
        try:
            from gpiozero import MotionSensor
        except ImportError:
            log.error("gpiozero fehlt – PIR deaktiviert")
            return
        self.pir = MotionSensor(int(pin))
        self.pir.when_motion = self.motion
        log.info("PIR an GPIO %s aktiv", pin)

    # ---------- Hauptschleife ----------

    def run(self):
        self.start_pir()
        failures = 0
        while not self.stopping:
            try:
                self.ensure_chromium()
                now = time.time()

                if self.override_url and self.override_until and now >= self.override_until:
                    self.override_url = None
                    self.show_index(self.index)
                elif not self.override_url and self.cfg["rotating"] and now >= self.next_switch:
                    full = now - self.last_full_reload > self.cfg["full_reload_hours"] * 3600
                    self.show_index(self.index + 1, full_reload=full)
                elif not self.current_url and self.cfg["views"]:
                    self.show_index(self.index)

                timeout = self.cfg["pir_timeout"]
                if self.cfg["pir_pin"] is not None and self.display_on and now - self.last_motion > timeout:
                    self.set_display(False, "keine Bewegung")
                failures = 0
            except Exception as e:  # nie abstürzen – kurz warten und neu versuchen
                failures += 1
                self.last_error = str(e)
                log.error("Fehler: %s", e)
                self.cdp.close()
                time.sleep(min(30, 2 * failures))

            with self.wake:
                self.wake.wait(timeout=1.0)

    def stop(self):
        self.stopping = True
        self.poke()
        if self.chromium and self.chromium.poll() is None:
            self.chromium.terminate()

    # ---------- Zustand für die API ----------

    def state(self):
        view = self.current_view()
        now = time.time()
        remaining = None
        if self.override_url:
            remaining = max(0, int(self.override_until - now)) if self.override_until else None
        elif self.cfg["rotating"] and view:
            remaining = max(0, int(self.next_switch - now))
        return {
            "views": self.cfg["views"],
            "index": self.index,
            "current": view,
            "current_url": self.current_url,
            "override": self.override_url,
            "rotating": self.cfg["rotating"],
            "interval": self.cfg["interval"],
            "view_seconds": self.view_seconds(view) if view else None,
            "remaining": remaining,
            "zoom": self.cfg["zoom"],
            "hide_ha_chrome": self.cfg["hide_ha_chrome"],
            "display_on": self.display_on,
            "pir": self.cfg["pir_pin"] is not None,
            "pir_timeout": self.cfg["pir_timeout"],
            "browser_ok": self.cdp.alive(),
            "error": self.last_error,
        }


# --------------------------------------------------------------------------
# REST-API
# --------------------------------------------------------------------------


def clean_views(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("views muss eine Liste sein")
    views = []
    for v in raw:
        url = str(v.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Ungültige URL: {url!r}")
        item = {"name": str(v.get("name") or url).strip()[:60], "url": url}
        if v.get("seconds"):
            item["seconds"] = max(5, min(3600, int(v["seconds"])))
        views.append(item)
    return views


def make_handler(kiosk: Kiosk):
    cfg = kiosk.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "kiosk/1.0"

        def log_message(self, fmt, *args):
            log.debug("API %s", fmt % args)

        def send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            sent = self.headers.get("X-Kiosk-Token", "")
            return secrets.compare_digest(sent, cfg["token"])

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            if self.path == "/health":
                return self.send(200, {"ok": True})
            if not self.authorized():
                return self.send(401, {"error": "Token fehlt oder ist falsch"})
            if self.path == "/api/state":
                return self.send(200, kiosk.state())
            if self.path == "/api/screenshot":
                try:
                    res = kiosk.cdp.call("Page.captureScreenshot",
                                         {"format": "jpeg", "quality": 55,
                                          "optimizeForSpeed": True}, timeout=8)
                    return self.send(200, base64.b64decode(res["data"]), "image/jpeg")
                except Exception as e:
                    return self.send(503, {"error": str(e)})
            self.send(404, {"error": "unbekannt"})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {"error": "Token fehlt oder ist falsch"})
            try:
                b = self.body()
                p = self.path
                if p == "/api/next":
                    kiosk.show_index(kiosk.index + 1)
                elif p == "/api/prev":
                    kiosk.show_index(kiosk.index - 1)
                elif p == "/api/goto":
                    kiosk.show_index(int(b["index"]))
                elif p == "/api/rotation":
                    running = bool(b.get("running"))
                    cfg.update(rotating=running)
                    if running:
                        view = kiosk.current_view()
                        kiosk.next_switch = time.time() + (kiosk.view_seconds(view) if view else 0)
                elif p == "/api/interval":
                    cfg.update(interval=max(5, min(3600, int(b["seconds"]))))
                elif p == "/api/zoom":
                    cfg.update(zoom=round(max(0.3, min(3.0, float(b["value"]))), 2))
                    kiosk.setup_page()
                elif p == "/api/hide_chrome":
                    cfg.update(hide_ha_chrome=bool(b.get("value")))
                    kiosk.setup_page()
                    kiosk.navigate(kiosk.current_url, full_reload=True)
                elif p == "/api/display":
                    kiosk.set_display(bool(b.get("on")), "Fernbedienung")
                    kiosk.last_motion = time.time()
                elif p == "/api/reload":
                    if kiosk.current_url:
                        kiosk.navigate(kiosk.current_url, full_reload=True)
                elif p == "/api/open":
                    url = str(b.get("url", "")).strip()
                    if not url.startswith(("http://", "https://")):
                        raise ValueError("URL muss mit http:// oder https:// beginnen")
                    hold = int(b.get("seconds") or 0)
                    kiosk.navigate(url)
                    kiosk.override_url = url
                    kiosk.override_until = time.time() + hold if hold > 0 else 0
                elif p == "/api/resume_views":
                    kiosk.show_index(kiosk.index)
                elif p == "/api/views":
                    views = clean_views(b.get("views"))
                    cfg.update(views=views)
                    kiosk.show_index(min(kiosk.index, max(0, len(views) - 1)))
                elif p == "/api/restart_browser":
                    if kiosk.chromium:
                        kiosk.chromium.terminate()
                    kiosk.cdp.close()
                else:
                    return self.send(404, {"error": "unbekannt"})
                kiosk.poke()
                self.send(200, kiosk.state())
            except (KeyError, ValueError, TypeError) as e:
                self.send(400, {"error": str(e)})
            except Exception as e:
                kiosk.last_error = str(e)
                self.send(500, {"error": str(e)})

    return Handler


# --------------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=os.environ.get("KIOSK_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Wayland-Umgebung absichern (fehlt z. B. unter PM2/systemd)
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")

    cfg = Config(CONFIG_FILE)
    kiosk = Kiosk(cfg)

    server = ThreadingHTTPServer(("0.0.0.0", int(cfg["api_port"])), make_handler(kiosk))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("API läuft auf Port %s", cfg["api_port"])

    def shutdown(*_):
        kiosk.stop()
        server.shutdown()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    kiosk.run()


if __name__ == "__main__":
    main()
