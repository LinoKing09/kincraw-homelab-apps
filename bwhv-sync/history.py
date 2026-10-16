# -*- coding: utf-8 -*-
"""Kleiner Import-Verlauf als JSON-Datei im Datenordner."""

import json
import os
import threading
from datetime import datetime

import config

_lock = threading.Lock()


def load() -> list:
    if not os.path.exists(config.HISTORY_PATH):
        return []
    try:
        with open(config.HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def add(entry: dict):
    with _lock:
        entries = load()
        entry = dict(entry, timestamp=datetime.now(config.TZ).strftime("%d.%m.%Y %H:%M"))
        entries.insert(0, entry)
        entries = entries[: config.HISTORY_LIMIT]
        config.ensure_data_dir()
        tmp = config.HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
        os.replace(tmp, config.HISTORY_PATH)


def last() -> dict:
    entries = load()
    return entries[0] if entries else None
