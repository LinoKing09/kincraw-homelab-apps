"""Ausgeblendete Tage/Stunden – dauerhaft gespeichert in data/exclusions.json.

  days          ISO-Daten ganzer Tage, die nicht in den Kalender sollen
  once          Schlüssel einzelner Stunden ("JJJJMMTT-<Perioden-ID>")
  rules         dauerhafte Regeln: Wochentag + Beginn + Fach (jede Woche)
  include_once  Ausnahme von einer Regel: diese eine Stunde doch eintragen
"""
import json
import os
import threading

import config

PATH = os.path.join(config.DATA_DIR, "exclusions.json")
_lock = threading.Lock()


def _empty():
    return {"days": [], "once": [], "include_once": [], "rules": []}


def load() -> dict:
    try:
        with open(PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return _empty()
    base = _empty()
    base.update({k: d.get(k, v) for k, v in base.items()})
    return base


def _save(d: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def rule_id(weekday: int, start: str, subject: str) -> str:
    return f"{weekday}|{start}|{subject}"


def reason(ex: dict, key: str, day_iso: str, rid: str):
    """Warum eine Stunde ausgeblendet ist: 'day' | 'once' | 'rule' | None."""
    if day_iso in ex["days"]:
        return "day"
    if key in ex["once"]:
        return "once"
    if rid in {r["id"] for r in ex["rules"]} and key not in ex["include_once"]:
        return "rule"
    return None


def apply(action: str, p: dict) -> dict:
    with _lock:
        ex = load()

        def add(lst, v):
            if v and v not in lst:
                lst.append(v)

        def rm(lst, v):
            if v in lst:
                lst.remove(v)

        if action == "day_add":
            add(ex["days"], p.get("day"))
        elif action == "day_remove":
            rm(ex["days"], p.get("day"))
        elif action == "once_add":
            add(ex["once"], p.get("key"))
        elif action == "once_remove":
            rm(ex["once"], p.get("key"))
        elif action == "include_once_add":
            add(ex["include_once"], p.get("key"))
        elif action == "include_once_remove":
            rm(ex["include_once"], p.get("key"))
        elif action == "rule_add":
            rid = rule_id(int(p["weekday"]), p["start"], p["subject"])
            if rid not in {r["id"] for r in ex["rules"]}:
                ex["rules"].append({"id": rid, "weekday": int(p["weekday"]), "start": p["start"],
                                    "subject": p["subject"], "label": p.get("label") or p["subject"]})
            # alte Einzel-Ausnahmen dieser Regel sind damit erledigt
            ex["include_once"] = [k for k in ex["include_once"] if k not in p.get("keys", [])]
        elif action == "rule_remove":
            ex["rules"] = [r for r in ex["rules"] if r["id"] != p.get("id")]
        else:
            raise ValueError(f"Unbekannte Aktion: {action}")
        _save(ex)
        return ex


def prune(today_iso: str):
    """Vergangene Tage/Stunden-Einträge aufräumen (Regeln bleiben)."""
    today_key = today_iso.replace("-", "")
    with _lock:
        ex = load()
        before = json.dumps(ex, sort_keys=True)
        ex["days"] = [d for d in ex["days"] if d >= today_iso]
        ex["once"] = [k for k in ex["once"] if k[:8] >= today_key]
        ex["include_once"] = [k for k in ex["include_once"] if k[:8] >= today_key]
        if json.dumps(ex, sort_keys=True) != before:
            _save(ex)
