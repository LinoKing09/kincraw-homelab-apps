"""Wochen, die es in WebUntis noch nicht gibt, aus dem letzten bekannten
Stundenplan auffüllen ("vorläufige" Stunden).

Aufgefüllt wird ab dem Tag nach dem letzten Tag, für den WebUntis Stunden
liefert – also auch der Rest einer nur teilweise veröffentlichten Woche.
Leere Tage/Wochen davor (z. B. Ferien) bleiben leer, und Ferien-/Feiertage
laut WebUntis werden übersprungen.

Vorlage ist je Wochentag die jüngste Woche mit echten Stunden an diesem Tag –
bereinigt um einmalige Abweichungen: Entfall zählt als normale Stunde, eine
Vertretung, die sich mit einer entfallenen Stunde überschneidet, wird weggelassen.
"""
from datetime import date, datetime, timedelta

from untis import Lesson, alt_key


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _is_holiday(d: date, holidays) -> bool:
    return any(a <= d <= b for a, b in holidays or [])


def _clean_day(lessons: list) -> list:
    """Stunden eines Tages als Vorlage-Einträge (ohne einmalige Abweichungen)."""
    cancelled = [l for l in lessons if l.status == "cancelled"]
    out = []
    for l in lessons:
        if l.status == "irregular" and any(c.start < l.end and l.start < c.end for c in cancelled):
            continue   # Ersatz für eine entfallene Stunde – die reguläre zählt
        out.append({
            "weekday": l.day.weekday(),
            "start": l.start.strftime("%H:%M"), "end": l.end.strftime("%H:%M"),
            "subject": l.subject, "subject_long": l.subject_long,
            "rooms": list(l.rooms), "teachers": list(l.teachers), "classes": list(l.classes),
        })
    return out


def build_template(real: list, holidays):
    """Vorlage je Wochentag: die jüngste Woche, in der es an diesem Wochentag
    echte Stunden gibt. So stört eine nur teilweise veröffentlichte Woche
    (z. B. WebUntis zeigt erst den Montag) oder ein Feiertag die Vorlage nicht.
    None, wenn es gar keine Stunden gibt."""
    by_week = {}
    for l in real:
        by_week.setdefault(monday(l.day), {}).setdefault(l.day.weekday(), []).append(l)
    if not by_week:
        return None
    weeks = sorted(by_week, reverse=True)
    entries, used = [], set()
    for wd in range(7):
        week = next((w for w in weeks if by_week[w].get(wd)), None)
        if week is None:
            continue
        used.add(week)
        entries += _clean_day(by_week[week][wd])
    return {"source_week": max(used).isoformat(), "lessons": entries}


def fill_missing(real: list, week_start: date, end: date, holidays, stored_template=None):
    """Gibt (alle Stunden inkl. vorläufiger, Vorlage, Anzahl vorläufiger) zurück.

    Aufgefüllt wird ab dem Tag nach dem letzten Tag mit echten Stunden –
    auch der Rest einer nur teilweise veröffentlichten Woche.

    holidays: Liste von (erster Tag, letzter Tag) oder None, wenn unbekannt.
    stored_template: zuletzt gespeicherte Vorlage – wird genutzt, wenn WebUntis
    im ganzen Zeitraum gar nichts liefert."""
    template = build_template(real, holidays) or stored_template
    if not template or not template.get("lessons"):
        return list(real), template, 0

    if real:
        first_missing = max(l.day for l in real) + timedelta(days=1)
    else:
        first_missing = week_start
    first_missing = max(first_missing, week_start)

    by_wd = {}
    for e in template["lessons"]:
        by_wd.setdefault(e["weekday"], []).append(e)

    projected = []
    day = first_missing
    while day <= end:
        if not _is_holiday(day, holidays):
            for e in by_wd.get(day.weekday(), []):
                start = datetime.strptime(e["start"], "%H:%M").time()
                projected.append(Lesson(
                    key=alt_key(day, start, e["subject"]), day=day, start=start,
                    end=datetime.strptime(e["end"], "%H:%M").time(),
                    subject=e["subject"], subject_long=e["subject_long"],
                    rooms=list(e["rooms"]), teachers=list(e["teachers"]), classes=list(e["classes"]),
                    projected=True,
                ))
        day += timedelta(days=1)

    out = list(real) + projected
    out.sort(key=lambda l: (l.day, l.start, l.subject))
    return out, template, len(projected)
