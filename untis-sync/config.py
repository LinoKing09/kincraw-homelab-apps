"""Zentrale Konfiguration – alles über Umgebungsvariablen (.env) steuerbar."""
import os
import secrets


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ---- WebUntis -------------------------------------------------------------
# Aus der WebUntis-Adresse ablesen, z. B.
#   https://nessa.webuntis.com/WebUntis/?school=gym-beispiel#/basic/login
#   -> UNTIS_SERVER=nessa.webuntis.com, UNTIS_SCHOOL=gym-beispiel
UNTIS_SERVER = _env("UNTIS_SERVER")
UNTIS_SCHOOL = _env("UNTIS_SCHOOL")
UNTIS_USER = _env("UNTIS_USER")
UNTIS_PASSWORD = _env("UNTIS_PASSWORD")

# ---- Google Calendar --------------------------------------------------------
# "primary" = dein Hauptkalender. Das Label muss auf GENAU diesem Kalender
# angelegt sein (Labels sind pro Kalender definiert).
CALENDAR_ID = _env("GCAL_CALENDAR_ID", "primary")
LABEL_NAME = _env("GCAL_LABEL_NAME", "Schule")
COLOR_FALLBACK = _env("GCAL_COLOR_FALLBACK", "9")  # Blueberry, nur falls Label fehlt
# Erinnerungen pro Stunde: leer = keine (sonst klingelt es vor jeder Stunde).
# Z. B. "10" für 10 Minuten vorher.
REMINDER_MINUTES = _env("REMINDER_MINUTES", "")
USE_LONG_NAMES = _env("USE_LONG_SUBJECT_NAMES", "1") == "1"

# ---- Sync-Verhalten ---------------------------------------------------------
TIMEZONE = _env("TIMEZONE", "Europe/Berlin")
# Die nächsten beiden Werte sind nur Startwerte – in der Oberfläche unter
# "Einstellungen" änderbar (gespeichert in data/settings.json).
WEEKS_AHEAD = int(_env("SYNC_WEEKS_AHEAD", "4"))
FILL_MISSING = _env("FILL_MISSING_WEEKS", "1") == "1"
SYNC_INTERVAL_MIN = int(_env("SYNC_INTERVAL_MINUTES", "120"))
CANCELLED_MODE = _env("CANCELLED_MODE", "mark")  # mark | delete
MERGE_DOUBLE = _env("MERGE_DOUBLE_LESSONS", "1") == "1"
MERGE_GAP_MIN = int(_env("MERGE_GAP_MINUTES", "5"))
# Tage, die getTimetable noch nicht liefert, über die neue WebUntis-Schnittstelle
# (wie Web-Oberfläche/App) holen. 0 = aus.
USE_REST = _env("UNTIS_USE_REST", "1") == "1"

# ---- Dateien ----------------------------------------------------------------
DATA_DIR = _env("DATA_DIR", "/data")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CLIENT_SECRET_PATH = _env("CLIENT_SECRET_PATH", "/app/client_secret.json")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
SECRET_KEY_PATH = os.path.join(DATA_DIR, "secret_key")

# ---- Zugangsschutz für die Oberfläche ----------------------------------------
# Nicht verwechseln mit UNTIS_PASSWORD (das ist der WebUntis-Login)!
# Ist APP_PASSWORD gesetzt, verlangt die Oberfläche eine Anmeldung. Leer = offen.
APP_PASSWORD = _env("APP_PASSWORD", "")
LOGIN_DAYS = int(_env("APP_LOGIN_DAYS", "90"))


def get_secret_key() -> str:
    """Schlüssel für das Anmelde-Cookie; wird einmalig in data/secret_key erzeugt,
    damit Anmeldungen einen Neustart überstehen."""
    env = _env("APP_SECRET_KEY")
    if env:
        return env
    try:
        with open(SECRET_KEY_PATH) as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    return key


def untis_configured() -> bool:
    return all([UNTIS_SERVER, UNTIS_SCHOOL, UNTIS_USER, UNTIS_PASSWORD])
