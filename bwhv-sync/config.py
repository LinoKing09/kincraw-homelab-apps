# -*- coding: utf-8 -*-
"""Zentrale Konfiguration. Alles Veränderliche liegt im Datenordner (DATA_DIR),
damit Container-Rebuilds, Updates und Umzüge (Pi -> Proxmox) nichts verlieren."""

import os
import secrets
import shutil
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BWHV_DATA_DIR", os.path.join(BASE_DIR, "data"))

TIMEZONE = os.environ.get("BWHV_TIMEZONE", "Europe/Berlin")
TZ = ZoneInfo(TIMEZONE)
CALENDAR_NAME = os.environ.get("BWHV_CALENDAR_NAME", "BWHV Schiri FWL")
MARKER_LABEL_NAME = os.environ.get("BWHV_MARKER_LABEL", "FWL fertig")

TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
CLIENT_SECRET_PATH = os.path.join(DATA_DIR, "client_secret.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
SECRET_KEY_PATH = os.path.join(DATA_DIR, "secret_key")

HISTORY_LIMIT = 30


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    # Migration vom alten Layout: client_secret.json / token.json lagen früher
    # direkt im Projektordner. Einmalig in den Datenordner übernehmen.
    for name in ("client_secret.json", "token.json"):
        old = os.path.join(BASE_DIR, name)
        new = os.path.join(DATA_DIR, name)
        if os.path.isfile(old) and os.path.getsize(old) > 0 and not os.path.exists(new):
            shutil.copy2(old, new)


def get_secret_key() -> str:
    env = os.environ.get("BWHV_SECRET_KEY")
    if env and env != "aendere-mich-bitte":
        return env
    ensure_data_dir()
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH) as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(SECRET_KEY_PATH), exist_ok=True)
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    return key
