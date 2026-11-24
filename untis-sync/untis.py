"""Minimaler WebUntis-JSON-RPC-Client + Aufbereitung der Stunden."""
import base64
import copy
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta

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

    # -------------------------------------------- neue REST-Schnittstelle ----
    # Die Web-Oberfläche und die Untis-App nutzen eine neuere Schnittstelle.
    # Sie zeigt oft weiter in die Zukunft als getTimetable (bei manchen Schulen
    # zählt die Freigabe dort Schultage statt Kalendertage).

    def _rest_headers(self) -> dict:
        base = f"https://{config.UNTIS_SERVER}/WebUntis"
        try:
            r = self.s.get(f"{base}/api/token/new", timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            raise UntisError(f"Neue WebUntis-Schnittstelle: kein Token ({e})") from e
        token = r.text.strip().strip('"')
        if not token or token.startswith("<"):
            raise UntisError("Neue WebUntis-Schnittstelle: kein Token erhalten.")
        h = {"Authorization": f"Bearer {token}"}
        try:   # manche Schulen verlangen zusätzlich die Mandanten-ID
            tid = (self.s.get(f"{base}/api/rest/view/v1/app/data", headers=h, timeout=20)
                   .json().get("tenant") or {}).get("id")
            if tid:
                h["Tenant-Id"] = str(tid)
        except (requests.RequestException, ValueError, AttributeError):
            pass
        return h

    def rest_entries(self, start: date, end: date) -> list:
        """Rohe Einträge der neuen Schnittstelle als Liste von (Tag, Eintrag),
        wochenweise abgefragt (wie die Web-Oberfläche)."""
        base = f"https://{config.UNTIS_SERVER}/WebUntis"
        headers = self._rest_headers()
        out = []
        week = start - timedelta(days=start.weekday())
        while week <= end:
            params = {"start": max(week, start).isoformat(),
                      "end": min(week + timedelta(days=6), end).isoformat(),
                      "format": 4, "resourceType": "STUDENT", "resources": self.person_id,
                      "periodTypes": "", "timetableType": "MY_TIMETABLE", "layout": "START_TIME"}
            try:
                r = self.s.get(f"{base}/api/rest/view/v1/timetable/entries",
                               params=params, headers=headers, timeout=20)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                raise UntisError(f"Neue WebUntis-Schnittstelle: {e}") from e
            for d in (data.get("days") or []) if isinstance(data, dict) else []:
                try:
                    day = date.fromisoformat(str(d.get("date"))[:10])
                except ValueError:
                    continue
                out += [(day, e) for e in d.get("gridEntries") or []]
            week += timedelta(days=7)
        return out

    def holidays(self):
        """Ferien/Feiertage als Liste von (erster Tag, letzter Tag).
        None, wenn das Konto sie nicht abfragen darf."""
        try:
            raw = self._call("getHolidays") or []
        except UntisError:
            return None
        out = []
        for h in raw:
            try:
                out.append((datetime.strptime(str(h["startDate"]), "%Y%m%d").date(),
                            datetime.strptime(str(h["endDate"]), "%Y%m%d").date()))
            except (KeyError, ValueError):
                continue
        return out


# ------------------------------------------------------- Aufbereitung ----

def alt_key(day: date, start: time, subject: str) -> str:
    """Schlüssel ohne Untis-ID: Datum + Beginn + Fach. Ist der Schlüssel
    vorläufiger (kopierter) Stunden; echte Stunden kennen ihn als Zweitschlüssel,
    damit ein "nur diesmal entfernen" bestehen bleibt, sobald WebUntis die
    Woche veröffentlicht."""
    return f"{day:%Y%m%d}-p{start:%H%M}-{subject}"


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
    projected: bool = False  # True = aus dem letzten Stundenplan kopiert (noch nicht in WebUntis)
    source: str = "rpc"      # rpc = getTimetable, rest = neue Schnittstelle, copy = aufgefüllt
    display: str = ""        # Titel in der gewählten Schreibweise (names.apply)

    @property
    def alt_key(self) -> str:
        return alt_key(self.day, self.start, self.subject)

    @property
    def title(self) -> str:
        if self.display:
            return self.display
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
        d["alt_key"] = self.alt_key
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


# Einträge der neuen Schnittstelle, die kein Unterricht sind
_REST_SKIP_TYPES = {"BREAK_SUPERVISION", "OFFICE_HOUR", "STANDBY"}


def _rest_elements(entry: dict, kind: str) -> list:
    """Alle Elemente eines Typs (TEACHER/SUBJECT/ROOM/CLASS) aus position1..7."""
    out = []
    for i in range(1, 8):
        for el in entry.get(f"position{i}") or []:
            cur, rem = el.get("current"), el.get("removed")
            if (cur or rem or {}).get("type") == kind:
                out.append((cur, rem))
    return out


def _rest_text(v) -> list:
    if not v:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, dict):
        return _rest_text(v.get("text") or v.get("value"))
    if isinstance(v, list):
        return [t for x in v for t in _rest_text(x)]
    return []


def normalize_rest(entries: list) -> list:
    """(Tag, Eintrag) der neuen Schnittstelle -> Lesson (wie normalize)."""
    out, seen = [], set()
    for day, e in entries:
        typ = str(e.get("type") or "").upper()
        if typ in _REST_SKIP_TYPES:
            continue
        dur = e.get("duration") or {}
        try:
            start = datetime.fromisoformat(dur["start"]).time()
            end = datetime.fromisoformat(dur["end"]).time()
        except (KeyError, TypeError, ValueError):
            continue
        ids = e.get("ids") or []
        key = f"{day:%Y%m%d}-{ids[0]}" if ids else alt_key(day, start, "x")
        if key in seen:
            continue
        seen.add(key)

        st = str(e.get("status") or "REGULAR").upper()
        if "CANCEL" in st:
            status = "cancelled"
        elif "EXAM" in typ or "EXAM" in st:
            status = "exam"
        elif st != "REGULAR" or e.get("moved"):
            status = "irregular"
        else:
            status = "normal"

        subs = [c for c, _ in _rest_elements(e, "SUBJECT") if c]
        if subs:
            subject = subs[0].get("shortName") or subs[0].get("displayName") or "Unterricht"
            subject_long = subs[0].get("longName") or subject
        else:
            subject = subject_long = (e.get("name") or (_rest_text(e.get("lessonText")) or [None])[0]
                                      or "Unterricht")

        def names(kind):
            return [c.get("shortName") or c.get("displayName")
                    for c, _ in _rest_elements(e, kind) if c and (c.get("shortName") or c.get("displayName"))]

        def changed(kind):
            return any(r is not None or str((c or {}).get("status") or "REGULAR").upper() != "REGULAR"
                       for c, r in _rest_elements(e, kind))

        info = []
        for t in (_rest_text(e.get("substitutionText")) + _rest_text(e.get("lessonText"))
                  + _rest_text(e.get("lessonInfo")) + _rest_text(e.get("texts"))
                  + _rest_text(e.get("notesAll"))):
            if t not in info and t != subject_long:
                info.append(t)

        out.append(Lesson(
            key=key, day=day, start=start, end=end,
            subject=subject, subject_long=subject_long,
            rooms=names("ROOM"), teachers=names("TEACHER"), classes=names("CLASS"),
            status=status, room_changed=changed("ROOM"), teacher_changed=changed("TEACHER"),
            info=info, source="rest",
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
