# -*- coding: utf-8 -*-
"""
Parser für die BWHV "Freiwunschliste" PDF (Phoenix / handball4all Export).

Erwartetes Format:

    Freiwunschliste ganzjährig geöffnet
    letzte Änderung: 14.08.2026 00:19

    Generelle Sperrzeiten
    Mo: 06:00 - 17:59
    ...
    Sperrzeiträume
    01.08.2026 - 08.08.2026
    ...
    weitere Sperrtermine
    12.09.2026, 00:00 Uhr bis 23:59 Uhr (keine Zeit)
    ...

Funktioniert auch mit mehrseitigen PDFs: Seitenfüße ("Seite 1/2") und die auf
Folgeseiten wiederholten Kopfzeilen werden vor dem Zerlegen entfernt.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import Optional

import pdfplumber

WEEKDAY_MAP = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3, "Fr": 4, "Sa": 5, "So": 6}
WEEKDAY_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

HEADINGS = ("Generelle Sperrzeiten", "Sperrzeiträume", "weitere Sperrtermine")


class ParseError(ValueError):
    """PDF ist keine Freiwunschliste im erwarteten Format."""


@dataclass
class GenerelleSperrzeit:
    weekday_abbr: str
    weekday_index: int
    start: time
    end: time


@dataclass
class Sperrzeitraum:
    start_date: date
    end_date: date


@dataclass
class Sperrtermin:
    the_date: date
    start: time
    end: time
    note: Optional[str] = None  # Inhalt der Klammer, z. B. "keine Zeit"


@dataclass
class Freiwunschliste:
    last_change: Optional[datetime]
    generelle_sperrzeiten: list = field(default_factory=list)
    sperrzeitraeume: list = field(default_factory=list)
    weitere_sperrtermine: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.generelle_sperrzeiten or self.sperrzeitraeume or self.weitere_sperrtermine)


def _parse_time(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%d.%m.%Y").date()


def extract_text(pdf_path: str) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:  # beschädigt / kein PDF
        raise ParseError(f"Die Datei konnte nicht als PDF gelesen werden ({e}).") from e


def _clean(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"Seite\s+\d+\s*/\s*\d+", s):
            continue  # Seitenfuß
        lines.append(s)
    return "\n".join(lines)


def _sections(text: str) -> dict:
    """Zerlegt den Text an den drei Überschriften. Ein Abschnitt reicht bis
    zur nächsten Überschrift bzw. bis zum Textende (auch über Seiten hinweg)."""
    positions = []
    for h in HEADINGS:
        idx = text.find(h)
        if idx != -1:
            positions.append((idx, h))
    positions.sort()
    out = {h: "" for h in HEADINGS}
    for i, (idx, h) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        out[h] = text[idx + len(h):end]
    return out


def parse_text(raw_text: str) -> Freiwunschliste:
    if "Freiwunschliste" not in raw_text and not any(h in raw_text for h in HEADINGS):
        raise ParseError(
            "Das sieht nicht nach einer Freiwunschliste aus. "
            "Bitte die PDF über 'Freiwunschliste als PDF runterladen' exportieren."
        )
    text = _clean(raw_text)

    last_change = None
    m = re.search(r"letzte Änderung:\s*(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})", text)
    if m:
        last_change = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M")

    sec = _sections(text)
    result = Freiwunschliste(last_change=last_change)

    seen_wd = set()
    for wd, s, e in re.findall(r"\b(Mo|Di|Mi|Do|Fr|Sa|So):\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})",
                               sec["Generelle Sperrzeiten"]):
        if wd in seen_wd:
            continue
        seen_wd.add(wd)
        result.generelle_sperrzeiten.append(
            GenerelleSperrzeit(wd, WEEKDAY_MAP[wd], _parse_time(s), _parse_time(e)))

    for s, e in re.findall(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", sec["Sperrzeiträume"]):
        result.sperrzeitraeume.append(Sperrzeitraum(_parse_date(s), _parse_date(e)))

    for d, s, e, note in re.findall(
        r"(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})\s*Uhr\s*bis\s*(\d{2}:\d{2})\s*Uhr(?:\s*\(([^)]*)\))?",
        sec["weitere Sperrtermine"],
    ):
        result.weitere_sperrtermine.append(
            Sperrtermin(_parse_date(d), _parse_time(s), _parse_time(e), note.strip() if note else None))

    return result


def parse_freiwunschliste(pdf_path: str) -> Freiwunschliste:
    return parse_text(extract_text(pdf_path))


if __name__ == "__main__":
    import sys
    fw = parse_freiwunschliste(sys.argv[1])
    print("letzte Änderung:", fw.last_change)
    for x in fw.generelle_sperrzeiten:
        print("  generell:", x)
    for x in fw.sperrzeitraeume:
        print("  zeitraum:", x)
    for x in fw.weitere_sperrtermine:
        print("  termin:  ", x)
