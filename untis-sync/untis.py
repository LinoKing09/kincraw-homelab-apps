"""Minimaler WebUntis-JSON-RPC-Client + Aufbereitung der Stunden."""
import base64
import copy
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time

import requests

import config


class UntisError(Exception):
    pass


# Bekannte Fehlercodes der WebUntis-JSON-RPC-API
_ERROR_HINTS = {
    -8504: "Benutzername oder Passwort falsch.",
    -8520: "Nicht angemeldet (Session abgelaufen).",
    -8509: "Keine Berechtigung für den Stundenplan dieses Kontos.",
    -8998: "Benutzer ist gesperrt.",
}


class UntisClient:
    def __init__(self):
        if not config.untis_configured():
            raise UntisError("WebUntis-Zugangsdaten fehlen – bitte .env prüfen.")
        self.url = f"https://{config.UNTIS_SERVER}/WebUntis/jsonrpc.do"
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "untis-gcal-sync"
        school_b64 = base64.b64encode(config.UNTIS_SCHOOL.encode()).decode()
        self.s.cookies.set("schoolname", f'"_{school_b64}"')
        self.person_type = None
        self.person_id = None
        self._req_id = 0

    # ------------------------------------------------------------- RPC ----
    def _call(self, method: str, params=None):
        self._req_id += 1
        try:
            r = self.s.post(
                self.url,
                params={"school": config.UNTIS_SCHOOL},
                json={"id": str(self._req_id), "method": method,
                      "params": params or {}, "jsonrpc": "2.0"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise UntisError(f"WebUntis nicht erreichbar: {e}") from e
        except ValueError:
            raise UntisError("WebUntis hat keine gültige Antwort geliefert "
                             "(Server/Schulname in der .env prüfen).")
        if "error" in data:
            err = data["error"] or {}
            code = err.get("code")
            hint = _ERROR_HINTS.get(code, err.get("message", "Unbekannter Fehler"))
            raise UntisError(f"WebUntis ({method}): {hint} [Code {code}]")
        return data.get("result")

    def login(self):
        res = self._call("authenticate", {
            "user": config.UNTIS_USER,
            "password": config.UNTIS_PASSWORD,
            "client": "untis-gcal-sync",
        }) or {}
        self.person_type = res.get("personType")
        self.person_id = res.get("personId")
        if not self.person_id:
            raise UntisError("Login erfolgreich, aber WebUntis liefert keine "
                             "Personen-ID (z. B. Eltern- oder Anonym-Konto?).")

    def logout(self):
        try:
            self._call("logout")
        except UntisError:
            pass

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()

    def timetable(self, start: date, end: date) -> list:
        opts = {
            "element": {"id": self.person_id, "type": self.person_type},
            "startDate": int(start.strftime("%Y%m%d")),
            "endDate": int(end.strftime("%Y%m%d")),
            "showInfo": True,
            "showSubstText": True,
            "showLsText": True,
            "showStudentgroup": True,
            "klasseFields": ["id", "name"],
            "subjectFields": ["id", "name", "longname"],
            "roomFields": ["id", "name", "longname"],
            "teacherFields": ["id", "name", "longname"],
        }
        return self._call("getTimetable", {"options": opts}) or []


# ------------------------------------------------------- Aufbereitung ----

@dataclass
class Lesson:
    key: str            # stabil: Datum + Untis-Perioden-ID (keine Uhrzeit!)
    day: date
    start: time
    end: time
    subject: str
    subject_long: str
    rooms: list = field(default_factory=list)
    teachers: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    status: str = "normal"   # normal | cancelled | irregular | exam
    room_changed: bool = False
    teacher_changed: bool = False
    info: list = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.subject_long if config.USE_LONG_NAMES else self.subject

    @property
    def rule_id(self) -> str:
        return f"{self.day.weekday()}|{self.start.strftime('%H:%M')}|{self.subject}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["day"] = self.day.isoformat()
        d["start"] = self.start.strftime("%H:%M")
        d["end"] = self.end.strftime("%H:%M")
        d["title"] = self.title
        d["weekday"] = self.day.weekday()
        d["rule_id"] = self.rule_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lesson":
        fields = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        fields["day"] = date.fromisoformat(d["day"])
        fields["start"] = datetime.strptime(d["start"], "%H:%M").time()
        fields["end"] = datetime.strptime(d["end"], "%H:%M").time()
        return cls(**fields)


def _names(items):
    return [i.get("name") for i in items or [] if i.get("name")]


def _hhmm(n: int) -> time:
    return time(int(n) // 100, int(n) % 100)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def normalize(raw: list) -> list:
    out, seen = [], set()
    for p in raw:
        key = f"{p.get('date')}-{p.get('id')}"
        if key in seen or "startTime" not in p or "endTime" not in p:
            continue
        seen.add(key)

        code = p.get("code")
        if code == "cancelled":
            status = "cancelled"
        elif p.get("lstype") == "ex":
            status = "exam"
        elif code == "irregular":
            status = "irregular"
        else:
            status = "normal"

        su = p.get("su") or []
        lstext = p.get("lstext")
        if su:
            subject = su[0].get("name") or "Unterricht"
            subject_long = su[0].get("longname") or subject
        else:
            subject = subject_long = lstext or p.get("activityType") or "Unterricht"
            lstext = None  # schon als Titel verwendet

        ro = p.get("ro") or []
        te = p.get("te") or []
        info = []
        for txt in (p.get("substText"), lstext, p.get("info")):
            if txt and txt not in info:
                info.append(txt)

        out.append(Lesson(
            key=key,
            day=datetime.strptime(str(p["date"]), "%Y%m%d").date(),
            start=_hhmm(p["startTime"]),
            end=_hhmm(p["endTime"]),
            subject=subject,
            subject_long=subject_long,
            rooms=_names(ro),
            teachers=_names(te),
            classes=_names(p.get("kl")),
            status=status,
            room_changed=any("orgid" in r for r in ro),
            teacher_changed=any("orgid" in t for t in te),
            info=info,
        ))
    out.sort(key=lambda l: (l.day, l.start, l.subject))
    return out


def merge_double_lessons(lessons: list, gap_min: int) -> list:
    """Fasst direkt aufeinanderfolgende gleiche Stunden zu einem Termin zusammen.
    Der Schlüssel der ersten Stunde bleibt erhalten."""
    merged = []
    for l in lessons:
        prev = merged[-1] if merged else None
        if (prev and prev.day == l.day and prev.subject == l.subject
                and prev.rooms == l.rooms and prev.teachers == l.teachers
                and prev.status == l.status
                and 0 <= _minutes(l.start) - _minutes(prev.end) <= gap_min):
            prev.end = l.end
            prev.room_changed |= l.room_changed
            prev.teacher_changed |= l.teacher_changed
            prev.info += [i for i in l.info if i not in prev.info]
        else:
            merged.append(copy.deepcopy(l))
    return merged
