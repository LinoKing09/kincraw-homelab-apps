"""Zentrale Konfiguration – alles über Umgebungsvariablen (.env) steuerbar."""
import os


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
WEEKS_AHEAD = int(_env("SYNC_WEEKS_AHEAD", "4"))
SYNC_INTERVAL_MIN = int(_env("SYNC_INTERVAL_MINUTES", "120"))
CANCELLED_MODE = _env("CANCELLED_MODE", "mark")  # mark | delete
MERGE_DOUBLE = _env("MERGE_DOUBLE_LESSONS", "1") == "1"
MERGE_GAP_MIN = int(_env("MERGE_GAP_MINUTES", "5"))

# ---- Dateien ----------------------------------------------------------------
DATA_DIR = _env("DATA_DIR", "/data")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CLIENT_SECRET_PATH = _env("CLIENT_SECRET_PATH", "/app/client_secret.json")

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def untis_configured() -> bool:
    return all([UNTIS_SERVER, UNTIS_SCHOOL, UNTIS_USER, UNTIS_PASSWORD])
