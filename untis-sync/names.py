"""Fachnamen im Kalender: kurz oder lang – und beim Langnamen wahlweise in der
Schreibweise der alten (getTimetable, "rpc") oder der neuen Schnittstelle ("rest").

Beide Schnittstellen liefern teils unterschiedliche Langnamen (z. B. "Chemie" vs.
"CHEMIE"). Die App merkt sich je Kurzname, was jede Schnittstelle liefert
(state.json -> subject_names), und gibt allen Stunden einheitlich die gewählte
Schreibweise – egal, aus welcher Quelle die Stunde kommt.

Eigene Namen (settings.json -> custom_names, z. B. {"g2": "Geschichte"}) gehen
immer vor.
"""
import config

MODES = ("short", "rpc", "rest")


def default_mode() -> str:
    return "rpc" if config.USE_LONG_NAMES else "short"


def collect(lessons: list, known: dict) -> dict:
    """Kurzname -> Langname je Schnittstelle aus echten Stunden übernehmen."""
    for l in lessons:
        if l.source in ("rpc", "rest") and l.subject and l.subject_long:
            known.setdefault(l.source, {})[l.subject] = l.subject_long
    return known


def apply(lessons: list, mode: str, known: dict, custom: dict = None) -> None:
    custom = custom or {}
    for l in lessons:
        if custom.get(l.subject):
            l.display = custom[l.subject]
        elif mode == "short":
            l.display = l.subject
        else:
            l.display = (known.get(mode) or {}).get(l.subject) or l.subject_long


def subjects(known: dict, custom: dict = None) -> list:
    """Alle bekannten Fächer für die Oberfläche (alphabetisch nach Kürzel)."""
    custom = custom or {}
    rpc, rest = known.get("rpc") or {}, known.get("rest") or {}
    keys = sorted(set(rpc) | set(rest) | set(custom), key=str.lower)
    return [{"short": k, "rpc": rpc.get(k), "rest": rest.get(k), "custom": custom.get(k, "")} for k in keys]
