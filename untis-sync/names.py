"""Fachnamen im Kalender: kurz oder lang – und beim Langnamen wahlweise in der
Schreibweise der alten (getTimetable, "rpc") oder der neuen Schnittstelle ("rest").

Beide Schnittstellen liefern teils unterschiedliche Langnamen (z. B. "Chemie" vs.
"CHEMIE"). Die App merkt sich je Kurzname, was jede Schnittstelle liefert
(state.json -> subject_names), und gibt allen Stunden einheitlich die gewählte
Schreibweise – egal, aus welcher Quelle die Stunde kommt.
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


def apply(lessons: list, mode: str, known: dict) -> None:
    for l in lessons:
        if mode == "short":
            l.display = l.subject
        else:
            l.display = (known.get(mode) or {}).get(l.subject) or l.subject_long


def examples(known: dict, limit: int = 6) -> list:
    """Beispiele für die Oberfläche – unterschiedliche Schreibweisen zuerst."""
    rpc, rest = known.get("rpc") or {}, known.get("rest") or {}
    out = [{"short": k, "rpc": rpc.get(k), "rest": rest.get(k)} for k in sorted(set(rpc) | set(rest))]
    out.sort(key=lambda e: (not (e["rpc"] and e["rest"] and e["rpc"] != e["rest"]), e["short"].lower()))
    return out[:limit]
