"""Tests für Einstellungen, vorläufige Wochen und den Sync-Ablauf (ohne Netz)."""
import datetime as dt
import os
import sys
import tempfile

os.environ.update(DISABLE_SCHEDULER="1", DATA_DIR=tempfile.mkdtemp(prefix="untis-test-"),
                  UNTIS_SERVER="x", UNTIS_SCHOOL="s", UNTIS_USER="u", UNTIS_PASSWORD="p")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import app  # noqa: E402
import excl  # noqa: E402
import gcal  # noqa: E402
import project  # noqa: E402
import settings  # noqa: E402
import untis  # noqa: E402

MON = dt.date(2026, 11, 16)          # ein Montag


def raw(i, day, s, e, su, code=None):
    p = {"id": i, "date": int(day.strftime("%Y%m%d")), "startTime": s, "endTime": e,
         "su": [{"name": su, "longname": su + "-lang"}], "ro": [{"name": "R1"}]}
    if code:
        p["code"] = code
    return p


def week(mon, base):
    out = []
    for wd in range(5):
        d = mon + dt.timedelta(days=wd)
        out += [raw(base + wd * 10, d, 800, 845, "M"), raw(base + wd * 10 + 1, d, 845, 930, "M"),
                raw(base + wd * 10 + 2, d, 950, 1035, "D")]
    return out


def lessons(raws):
    return untis.merge_double_lessons(untis.normalize(raws), 5)


# ------------------------------------------------------------ project ----

def test_nichts_zu_fuellen_wenn_untis_alles_liefert():
    real = lessons(week(MON, 100) + week(MON + dt.timedelta(days=7), 200))
    out, tpl, n = project.fill_missing(real, MON, MON + dt.timedelta(days=13), [], None)
    assert n == 0 and len(out) == len(real)
    assert tpl["source_week"] == (MON + dt.timedelta(days=7)).isoformat()


def test_fehlende_wochen_werden_kopiert_bis_zum_ende():
    real = lessons(week(MON, 100))
    end = MON + dt.timedelta(days=7 + 2)          # Mittwoch der Folgewoche
    out, tpl, n = project.fill_missing(real, MON, end, [], None)
    proj = [l for l in out if l.projected]
    assert n == len(proj) == 6                    # Mo–Mi je Doppelstunde M + D
    assert {l.day for l in proj} == {MON + dt.timedelta(days=7 + i) for i in range(3)}
    m = next(l for l in proj if l.subject == "M")
    assert (m.start, m.end) == (dt.time(8, 0), dt.time(9, 30)) and m.key == m.alt_key


def test_ferien_und_luecken_bleiben_leer():
    real = lessons(week(MON, 100) + week(MON + dt.timedelta(days=14), 300))   # Woche 2 leer
    hol = [(MON + dt.timedelta(days=21), MON + dt.timedelta(days=22))]        # Mo+Di in Woche 4 frei
    out, _, n = project.fill_missing(real, MON, MON + dt.timedelta(days=27), hol, None)
    proj_days = {l.day for l in out if l.projected}
    assert not any(MON + dt.timedelta(days=7) <= d < MON + dt.timedelta(days=14) for d in proj_days)
    assert proj_days == {MON + dt.timedelta(days=21 + i) for i in (2, 3, 4)}


def test_vorlage_bereinigt_entfall_und_vertretung():
    rs = week(MON, 100)
    rs[2]["code"] = "cancelled"                               # Mo D entfällt …
    rs.append(raw(999, MON, 950, 1035, "E", "irregular"))     # … dafür Vertretung E
    real = lessons(rs)
    out, _, _ = project.fill_missing(real, MON, MON + dt.timedelta(days=7), [], None)
    mo = [l for l in out if l.projected and l.day == MON + dt.timedelta(days=7)]
    assert sorted(l.subject for l in mo) == ["D", "M"] and all(l.status == "normal" for l in mo)


def test_feiertag_in_vorlagewoche_nimmt_vorwoche():
    w2 = MON + dt.timedelta(days=7)
    rs = week(MON, 100) + [r for r in week(w2, 200) if r["date"] != int(w2.strftime("%Y%m%d"))]
    out, tpl, _ = project.fill_missing(lessons(rs), MON, w2 + dt.timedelta(days=7), [(w2, w2)], None)
    assert any(l.projected and l.day == w2 + dt.timedelta(days=7) for l in out)   # Montag wieder da


def test_gespeicherte_vorlage_wenn_untis_nichts_liefert():
    _, tpl, _ = project.fill_missing(lessons(week(MON, 100)), MON, MON + dt.timedelta(days=4), [], None)
    out, tpl2, n = project.fill_missing([], MON, MON + dt.timedelta(days=4), [], tpl)
    assert n == 10 and tpl2 == tpl


# --------------------------------------------------------------- excl ----

def test_einmal_entfernt_gilt_auch_fuer_echte_stunde():
    ex = {"days": [], "once": ["20261123-p0800-M"], "include_once": [], "rules": []}
    assert excl.reason(ex, "20261123-4711", "2026-11-23", "0|08:00|M", "20261123-p0800-M") == "once"
    assert excl.reason(ex, "20261123-4711", "2026-11-23", "0|08:00|M") is None


# ----------------------------------------------------------- settings ----

def test_settings_validierung():
    assert settings.update({"weeks": 8, "fill_missing": False}) == {"weeks": 8, "fill_missing": False}
    assert settings.load()["weeks"] == 8
    for bad in ({"weeks": 0}, {"weeks": 99}, {"weeks": "5"}, {"weeks": True}, {"fill_missing": "ja"}):
        with pytest.raises(ValueError):
            settings.update(bad)
    settings.update({"weeks": 4, "fill_missing": True})


# ------------------------------------------------ Ablauf mit Fake-Kalender

class _Exe:
    def __init__(self, f): self.f = f
    def execute(self, num_retries=0): return self.f()


class _Events:
    def __init__(self): self.db, self.n = {}, 0
    def list(self, **k):
        return _Exe(lambda: {"items": [e for e in self.db.values()
                                       if k["timeMin"] <= e["start"]["dateTime"] < k["timeMax"]]})
    def insert(self, calendarId, body, **k):
        self.n += 1; e = dict(body, id=str(self.n)); self.db[e["id"]] = e; return _Exe(lambda: e)
    def update(self, calendarId, eventId, body, **k):
        self.db[eventId] = dict(body, id=eventId); return _Exe(lambda: None)
    def delete(self, calendarId, eventId):
        self.db.pop(eventId); return _Exe(lambda: None)


class _Cals:
    def get(self, calendarId):
        return _Exe(lambda: {"labelProperties": {"eventLabels": [{"id": "L", "name": "Schule"}]}})


class _Svc:
    def __init__(self): self.ev = _Events()
    def events(self): return self.ev
    def calendars(self): return _Cals()


def test_ablauf_auffuellen_verkuerzen_und_echte_daten(monkeypatch):
    svc = _Svc()
    monkeypatch.setattr(gcal, "get_service", lambda: svc)
    today = app._now().date()
    mon = today - dt.timedelta(days=today.weekday())
    published = {"weeks": 1}        # so viele Wochen "kennt" WebUntis

    def fake_fetch(ws, end):
        rs = [r for w in range(published["weeks"]) for r in week(mon + dt.timedelta(days=7 * w), 1000 * (w + 1))]
        rs = [r for r in rs if ws.strftime("%Y%m%d") <= str(r["date"]) <= end.strftime("%Y%m%d")]
        return lessons(rs), []
    monkeypatch.setattr(app, "_fetch_lessons", fake_fetch)
    settings.update({"weeks": 3, "fill_missing": True})

    def days_in_cal():
        return sorted({e["start"]["dateTime"][:10] for e in svc.ev.db.values()})

    r = app.run_sync("test")
    assert r["ok"] and r["stats"]["projected"] > 0
    last_day = dt.date.fromisoformat(days_in_cal()[-1])
    assert today + dt.timedelta(days=14) < last_day <= today + dt.timedelta(days=21)
    assert any("Vorläufig" in e["description"] for e in svc.ev.db.values())

    # "nur diesmal" an einer vorläufigen Stunde nächster Woche …
    st = app.app.test_client().get("/api/status").get_json()
    pl = next(l for l in st["lessons"] if l["projected"] and l["subject"] == "M")
    excl.apply("once_add", {"key": pl["key"]})

    # … bleibt bestehen, wenn WebUntis die Woche veröffentlicht (neue Schlüssel)
    published["weeks"] = 4
    n_before = len(svc.ev.db)
    r = app.run_sync("test")
    assert r["ok"] and r["stats"]["projected"] == 0 and r["stats"]["excluded"] == 1
    assert len(svc.ev.db) == n_before - 1
    assert not any("Vorläufig" in e["description"] for e in svc.ev.db.values())
    assert len({e["extendedProperties"]["private"]["untis_key"] for e in svc.ev.db.values()}) == len(svc.ev.db)

    # Zeitraum verkürzen räumt dahinter auf
    settings.update({"weeks": 1})
    r = app.run_sync("test")
    assert r["ok"] and r["stats"]["deleted"] > 0
    assert dt.date.fromisoformat(days_in_cal()[-1]) <= today + dt.timedelta(days=7)

    # Auffüllen aus: nur echte Stunden
    published["weeks"] = 1
    settings.update({"weeks": 3, "fill_missing": False})
    r = app.run_sync("test")
    assert r["ok"] and r["stats"]["projected"] == 0
    assert dt.date.fromisoformat(days_in_cal()[-1]) <= mon + dt.timedelta(days=4)
    settings.update({"weeks": 4, "fill_missing": True})


def test_settings_endpoint():
    c = app.app.test_client()
    assert c.post("/api/settings", json={"weeks": 40}).status_code == 400
    d = c.post("/api/settings", json={"weeks": 6}).get_json()
    assert d["ok"] and d["weeks"] == 6
    assert c.get("/api/status").get_json()["weeks"] == 6
