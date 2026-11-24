"""Tests für die neue WebUntis-Schnittstelle (REST) und das Zusammenspiel mit getTimetable."""
import datetime as dt
import os
import sys
import tempfile

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="untis-test-"))
for k, v in {"UNTIS_SERVER": "x", "UNTIS_SCHOOL": "s", "UNTIS_USER": "u", "UNTIS_PASSWORD": "p"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as appmod  # noqa: E402
import config  # noqa: E402
import untis  # noqa: E402

D = dt.date(2026, 12, 1)


def el(kind, short, long=None, status="REGULAR"):
    return {"type": kind, "status": status, "shortName": short, "longName": long or short,
            "displayName": short, "displayNameLabel": "NAME"}


def entry(ids, start, end, subject, teacher="ABC", room="1.01", status="REGULAR", typ="NORMAL_TEACHING_PERIOD",
          teacher_removed=None, room_removed=None, **extra):
    e = {"ids": ids, "duration": {"start": f"{D}T{start}", "end": f"{D}T{end}"},
         "type": typ, "status": status, "statusDetail": None, "name": None,
         "position1": [{"current": el("TEACHER", teacher, "Lehrkraft"), "removed": teacher_removed}],
         "position2": [{"current": el("SUBJECT", subject, subject.upper()), "removed": None}],
         "position3": [{"current": el("ROOM", room, "Unterrichtsraum"), "removed": room_removed}],
         "position4": None, "texts": [], "lessonText": "", "lessonInfo": None,
         "substitutionText": "", "notesAll": "", "moved": None}
    e.update(extra)
    return e


def test_normalize_rest_wie_im_echten_format():
    ls = untis.normalize_rest([
        (D, entry([473736, 473739], "07:50", "09:20", "g2", room="1.03")),
        (D, entry([5], "09:45", "10:30", "M1", status="CANCELLED")),
        (D, entry([6], "10:30", "11:15", "d2", status="CHANGED", substitutionText="Vertretung",
                  teacher_removed=el("TEACHER", "XYZ"))),
        (D, entry([7], "11:25", "12:10", "PH", room="0.15", room_removed=el("ROOM", "0.14"))),
        (D, entry([8], "12:10", "12:55", "CH", typ="EXAM", lessonText="Klausur")),
        (D, entry([9], "13:00", "13:20", "AUF", typ="BREAK_SUPERVISION")),
    ])
    by = {l.subject: l for l in ls}
    g2 = by["g2"]
    assert g2.key == "20261201-473736"                 # wie getTimetable: Datum + erste Perioden-ID
    assert (g2.start, g2.end) == (dt.time(7, 50), dt.time(9, 20))
    assert g2.subject_long == "G2" and g2.rooms == ["1.03"] and g2.teachers == ["ABC"]
    assert g2.status == "normal" and not g2.room_changed and not g2.teacher_changed
    assert by["M1"].status == "cancelled"
    assert by["d2"].status == "irregular" and by["d2"].teacher_changed and "Vertretung" in by["d2"].info
    assert by["PH"].room_changed and by["PH"].status == "normal"
    assert by["CH"].status == "exam" and by["CH"].info == ["Klausur"]
    assert "AUF" not in by                              # Pausenaufsicht ist kein Unterricht


class FakeClient:
    """getTimetable liefert nur Montag, REST Montag bis Mittwoch (wie im echten Fall)."""
    calls = []

    def __init__(self, rest_fails=False):
        self.rest_fails = rest_fails

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def timetable(self, start, end):
        mon = dt.date(2026, 11, 30)
        return [{"id": 100, "date": int(mon.strftime("%Y%m%d")), "startTime": 750, "endTime": 920,
                 "su": [{"name": "g2", "longname": "g2"}], "ro": [{"name": "1.03"}], "te": [{"name": "ABC"}]}]

    def holidays(self):
        return []

    def rest_entries(self, start, end):
        FakeClient.calls.append((start, end))
        if self.rest_fails:
            raise untis.UntisError("Neue WebUntis-Schnittstelle: 401")
        out = []
        for i in range(3):     # Mo, Di, Mi
            day = dt.date(2026, 11, 30) + dt.timedelta(days=i)
            e = entry([200 + i], "09:45", "11:15", "M1")
            e["duration"] = {"start": f"{day}T09:45", "end": f"{day}T11:15"}
            out.append((day, e))
        return out


def test_rest_ergaenzt_nur_die_tage_danach(monkeypatch):
    FakeClient.calls.clear()
    monkeypatch.setattr(appmod, "UntisClient", lambda: FakeClient())
    lessons, _, rest_all = appmod._fetch_lessons(dt.date(2026, 11, 30), dt.date(2026, 12, 4))
    # ganze Woche abgefragt (für die Namen), übernommen aber nur ab Dienstag
    assert FakeClient.calls == [(dt.date(2026, 11, 30), dt.date(2026, 12, 4))]
    days = [(l.day.isoformat(), l.subject, l.source) for l in lessons]
    assert days == [("2026-11-30", "g2", "rpc"), ("2026-12-01", "M1", "rest"), ("2026-12-02", "M1", "rest")]
    assert len(rest_all) == 3


def test_rest_fehler_bricht_sync_nicht_ab(monkeypatch):
    monkeypatch.setattr(appmod, "UntisClient", lambda: FakeClient(rest_fails=True))
    lessons, _, _ = appmod._fetch_lessons(dt.date(2026, 11, 30), dt.date(2026, 12, 4))
    assert [l.subject for l in lessons] == ["g2"]


def test_rest_abschaltbar(monkeypatch):
    FakeClient.calls.clear()
    monkeypatch.setattr(config, "USE_REST", False)
    monkeypatch.setattr(appmod, "UntisClient", lambda: FakeClient())
    lessons, _, _ = appmod._fetch_lessons(dt.date(2026, 11, 30), dt.date(2026, 12, 4))
    assert FakeClient.calls == [] and len(lessons) == 1


# ------------------------------------------------------------ Fachnamen ----

def _l(subject, long, source):
    return untis.Lesson(key="k", day=D, start=dt.time(8), end=dt.time(9),
                        subject=subject, subject_long=long, source=source)


def test_fachnamen_untis_oder_eigene():
    import names
    known = names.collect([_l("CH", "Chemie", "rpc"), _l("CH", "CHEMIE", "rest"),
                           _l("M1", "M1", "rpc"), _l("NEU", "Neufach", "rest")], {})
    ls = [_l("CH", "Chemie", "rpc"), _l("CH", "CHEMIE", "rest"), _l("CH", "Chemie", "copy"),
          _l("NEU", "Neufach", "rest"), _l("g2", "g2", "rpc")]
    custom = {"g2": "Geschichte", "M1": "Mathe"}
    names.apply(ls, "untis", known, custom)               # eigene Namen werden ignoriert
    assert [l.title for l in ls] == ["Chemie", "Chemie", "Chemie", "Neufach", "g2"]
    names.apply(ls, "custom", known, custom)              # ohne eigenen Namen: wie untis
    assert [l.title for l in ls] == ["Chemie", "Chemie", "Chemie", "Neufach", "Geschichte"]
    sub = {s["short"]: s for s in names.subjects(known, custom)}
    assert sub["CH"] == {"short": "CH", "untis": "Chemie", "custom": ""}
    assert sub["g2"]["custom"] == "Geschichte" and sub["NEU"]["untis"] == "Neufach"


def test_alte_einstellung_wird_uebernommen(tmp_path, monkeypatch):
    import json
    import settings
    p = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "PATH", str(p))
    p.write_text(json.dumps({"names": "rest"}))
    assert settings.load()["names"] == "untis"
    p.write_text(json.dumps({"names": "rpc", "custom_names": {"g2": "Geschichte"}}))
    assert settings.load()["names"] == "custom"


def test_fachnamen_im_kalender_und_status(monkeypatch):
    import gcal
    import settings
    sent = {}

    def fake_sync(lessons, *a, **k):
        sent["titles"] = sorted({l.title for l in lessons})
        return {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    today = appmod._now().date()

    def fake_fetch(ws, end):
        rpc = untis.Lesson(key="a", day=today, start=dt.time(23, 0), end=dt.time(23, 45),
                           subject="CH", subject_long="CHEMIE")
        rest = untis.Lesson(key="b", day=today, start=dt.time(22, 0), end=dt.time(22, 45),
                            subject="CH", subject_long="CHEMIE", source="rest")
        return [rpc], [], [rest]
    monkeypatch.setattr(appmod, "_fetch_lessons", fake_fetch)
    monkeypatch.setattr(gcal, "sync", fake_sync)
    settings.update({"names": "untis", "fill_missing": False, "custom_names": {"CH": "Chemie"}})
    assert appmod.run_sync("test")["ok"] and sent["titles"] == ["CHEMIE"]
    settings.update({"names": "custom"})
    assert appmod.run_sync("test")["ok"] and sent["titles"] == ["Chemie"]
    st = appmod.app.test_client().get("/api/status").get_json()
    assert st["names"] == "custom"
    assert {"short": "CH", "untis": "CHEMIE", "custom": "Chemie"} in st["subjects"]
    assert settings.update({"custom_names": {"CH": ""}})["custom_names"] == {}
    settings.update({"names": "untis", "fill_missing": True})
