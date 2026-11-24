"""Fachnamen im Kalender.

Zwei Varianten (settings.json -> names):
  untis   Namen, wie WebUntis sie liefert (getTimetable; fehlt dort ein Fach,
          die neue Schnittstelle) – einheitlich für alle Termine, egal aus
          welcher Quelle die Stunde stammt
  custom  eigene Bezeichnungen (settings.json -> custom_names, z. B.
          {"g2": "Geschichte"}); Fächer ohne eigene Bezeichnung wie "untis"

Die App merkt sich je Kurzname, was beide Schnittstellen liefern
(state.json -> subject_names).
"""
import config

MODES = ("untis", "custom")
_LEGACY = {"rpc", "rest", "short"}     # frühere Auswahl (Kurz / Schnittstelle 1 / 2)


def default_mode() -> str:
    return "untis"


def normalize_mode(value, custom: dict) -> str:
    """Gespeicherten Wert auf die aktuellen Varianten abbilden."""
    if value in MODES:
        return value
    if value in _LEGACY:
        return "custom" if custom else "untis"
    return default_mode()


def collect(lessons: list, known: dict) -> dict:
    """Kurzname -> Langname je Schnittstelle aus echten Stunden übernehmen."""
    for l in lessons:
        if l.source in ("rpc", "rest") and l.subject and l.subject_long:
            known.setdefault(l.source, {})[l.subject] = l.subject_long
    return known


def untis_name(subject: str, known: dict, fallback: str) -> str:
    if not config.USE_LONG_NAMES:
        return subject
    return ((known.get("rpc") or {}).get(subject) or (known.get("rest") or {}).get(subject)
            or fallback or subject)


def apply(lessons: list, mode: str, known: dict, custom: dict = None) -> None:
    custom = (custom or {}) if mode == "custom" else {}
    for l in lessons:
        l.display = custom.get(l.subject) or untis_name(l.subject, known, l.subject_long)


def subjects(known: dict, custom: dict = None) -> list:
    """Alle bekannten Fächer für die Oberfläche (alphabetisch nach Kürzel)."""
    custom = custom or {}
    rpc, rest = known.get("rpc") or {}, known.get("rest") or {}
    keys = sorted(set(rpc) | set(rest) | set(custom), key=str.lower)
    return [{"short": k, "untis": untis_name(k, known, k), "custom": custom.get(k, "")} for k in keys]
