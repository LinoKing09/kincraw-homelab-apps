"""In der Oberfläche änderbare Einstellungen – gespeichert in data/settings.json.

Fehlt die Datei (oder ein Schlüssel), gelten die Werte aus der .env
(SYNC_WEEKS_AHEAD, FILL_MISSING_WEEKS)."""
import json
import os
import threading

import config
import names

PATH = os.path.join(config.DATA_DIR, "settings.json")
WEEKS_MIN, WEEKS_MAX = 1, 26
_lock = threading.Lock()


def _defaults() -> dict:
    return {"weeks": min(max(config.WEEKS_AHEAD, WEEKS_MIN), WEEKS_MAX),
            "fill_missing": config.FILL_MISSING,
            "names": names.default_mode(),
            "custom_names": {}}


def load() -> dict:
    s = _defaults()
    try:
        with open(PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return s
    if isinstance(d.get("weeks"), int) and WEEKS_MIN <= d["weeks"] <= WEEKS_MAX:
        s["weeks"] = d["weeks"]
    if isinstance(d.get("fill_missing"), bool):
        s["fill_missing"] = d["fill_missing"]
    if isinstance(d.get("custom_names"), dict):
        s["custom_names"] = {str(k): str(v) for k, v in d["custom_names"].items() if v}
    if "names" in d:
        s["names"] = names.normalize_mode(d["names"], s["custom_names"])
    return s


def update(p: dict) -> dict:
    """Übernimmt 'weeks', 'fill_missing', 'names' und/oder 'custom_names'
    (wird ergänzt: {"g2": "Geschichte"} setzt, {"g2": ""} entfernt);
    wirft ValueError bei Unsinn."""
    with _lock:
        s = load()
        if "weeks" in p:
            w = p["weeks"]
            if isinstance(w, bool) or not isinstance(w, int) or not WEEKS_MIN <= w <= WEEKS_MAX:
                raise ValueError(f"Wochen müssen zwischen {WEEKS_MIN} und {WEEKS_MAX} liegen.")
            s["weeks"] = w
        if "fill_missing" in p:
            if not isinstance(p["fill_missing"], bool):
                raise ValueError("fill_missing muss true oder false sein.")
            s["fill_missing"] = p["fill_missing"]
        if "names" in p:
            if p["names"] not in names.MODES:
                raise ValueError("Fachnamen: untis oder custom.")
            s["names"] = p["names"]
        if "custom_names" in p:
            if not isinstance(p["custom_names"], dict):
                raise ValueError("custom_names muss ein Objekt sein.")
            cn = dict(s["custom_names"])
            for k, v in p["custom_names"].items():
                k, v = str(k).strip(), str(v or "").strip()
                if not k or len(k) > 30 or len(v) > 60:
                    raise ValueError("Fachname zu lang (höchstens 60 Zeichen).")
                if v:
                    cn[k] = v
                else:
                    cn.pop(k, None)
            s["custom_names"] = cn
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, PATH)
        return s
