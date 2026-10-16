# -*- coding: utf-8 -*-
"""Tests ohne Google-Zugang: Parser, Abgleich-Logik (mit Fake-Kalender), API-Routen."""
import copy
import io
import itertools
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE = """Freitag, 14. August 2026
Max Mustermann
Freiwunschliste ganzjährig geöffnet
letzte Änderung: 14.08.2026 00:19
Generelle Sperrzeiten
Mo: 06:00 - 17:59
Di: 06:00 - 17:59
Do: 06:00 - 17:59
Fr: 06:00 - 15:59
Sperrzeiträume
01.08.2026 - 08.08.2026
20.08.2026 - 07.09.2026
weitere Sperrtermine
12.09.2026, 00:00 Uhr bis 23:59 Uhr (keine Zeit)
13.09.2026, 00:00 Uhr bis 23:59 Uhr (keine Zeit)
26.09.2026, 10:00 Uhr bis 16:59 Uhr
20.12.2026, 00:00 Uhr bis 23:59 Uhr
Seite 1/1
"""


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", str(tmp_path / "client_secret.json"))
    monkeypatch.setattr(config, "HISTORY_PATH", str(tmp_path / "history.json"))
    monkeypatch.setattr(config, "SECRET_KEY_PATH", str(tmp_path / "secret_key"))
    yield


# ------------------------------------------------------------------ Fake ---

class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeCalendar:
    def __init__(self):
        self.events = {}
        self.ids = itertools.count(1)
        self.calls = []

    # service-API
    def calendarList(self):
        return self

    def calendars(self):
        return self

    def get(self, calendarId):
        return _Req(lambda: {"labelProperties": {"eventLabels": [{"id": "L1", "name": "FWL fertig"}]}})

    def list(self, pageToken=None, privateExtendedProperty=None, **kw):
        if privateExtendedProperty is None:
            return _Req(lambda: {"items": [{"id": "cal1", "summary": "BWHV Schiri FWL"}]})
        k, v = privateExtendedProperty.split("=")
        return _Req(lambda: {"items": [copy.deepcopy(e) for e in self.events.values()
                                       if e.get("extendedProperties", {}).get("private", {}).get(k) == v]})

    def insert(self, calendarId, body, **kw):
        def f():
            eid = f"e{next(self.ids)}"
            self.events[eid] = dict(copy.deepcopy(body), id=eid)
            self.calls.append(("insert", body.get("summary")))
            return self.events[eid]
        return _Req(f)

    def update(self, calendarId, eventId, body):
        def f():
            self.events[eventId] = dict(copy.deepcopy(body), id=eventId)
            self.calls.append(("update", body.get("summary")))
        return _Req(f)

    def patch(self, calendarId, eventId, body):
        def f():
            self.events[eventId]["extendedProperties"] = copy.deepcopy(body["extendedProperties"])
            self.calls.append(("patch", eventId))
        return _Req(f)

    def delete(self, calendarId, eventId):
        def f():
            del self.events[eventId]
            self.calls.append(("delete", eventId))
        return _Req(f)


class FakeService:
    def __init__(self):
        self.cal = FakeCalendar()

    def events(self):
        return self.cal

    def calendars(self):
        return self.cal

    def calendarList(self):
        return self.cal


@pytest.fixture
def svc(monkeypatch):
    import gcal
    s = FakeService()
    gcal._calendar_id_cache.clear()
    monkeypatch.setattr(gcal, "get_service", lambda: s)
    monkeypatch.setattr(gcal, "today", lambda: date(2026, 9, 1))
    return s


# ---------------------------------------------------------------- Parser ---

def test_parser_sample():
    from parser import parse_text
    fw = parse_text(SAMPLE)
    assert len(fw.generelle_sperrzeiten) == 4
    assert len(fw.sperrzeitraeume) == 2
    assert len(fw.weitere_sperrtermine) == 4
    assert fw.weitere_sperrtermine[0].note == "keine Zeit"
    assert fw.last_change.strftime("%d.%m.%Y %H:%M") == "14.08.2026 00:19"


def test_parser_multipage():
    from parser import parse_text
    text = SAMPLE.replace("26.09.2026, 10:00", "Seite 1/2\nFreitag, 14. August 2026\nMax Mustermann\n26.09.2026, 10:00")
    text = text.replace("Seite 1/1", "Seite 2/2")
    fw = parse_text(text)
    assert len(fw.weitere_sperrtermine) == 4  # auch Einträge auf Seite 2


def test_parser_rejects_other_pdf():
    from parser import parse_text, ParseError
    with pytest.raises(ParseError):
        parse_text("Rechnung Nr. 123\nBetrag 12,00 EUR")


# ---------------------------------------------------------------- Abgleich -

def _sync(text, bis=date(2026, 12, 31)):
    import gcal
    from parser import parse_text
    return gcal.sync_freiwunschliste(parse_text(text), bis)


def _managed(svc, cat):
    return [e for e in svc.cal.events.values()
            if e["extendedProperties"]["private"].get("bwhv_category") == cat]


def test_first_and_repeat_import(svc):
    s1 = _sync(SAMPLE)
    assert s1["added"] == 10 and s1["updated"] == 0 and s1["deleted"] == 0
    s2 = _sync(SAMPLE)
    assert (s2["added"], s2["updated"], s2["deleted"], s2["unchanged"]) == (0, 0, 0, 10)
    assert len(_managed(svc, "marker")) == 1


def test_time_change_is_update_not_add_delete(svc):
    _sync(SAMPLE)
    s = _sync(SAMPLE.replace("26.09.2026, 10:00 Uhr bis 16:59", "26.09.2026, 12:00 Uhr bis 18:59"))
    assert (s["added"], s["updated"], s["deleted"]) == (0, 1, 0)
    upd = s["plan"]["update"][0]
    assert "10:00" in upd["previous"] and "12:00" in upd["when"]


def test_removed_entry_deleted_and_own_events_untouched(svc):
    _sync(SAMPLE)
    own = svc.cal.insert("cal1", {"summary": "Eigener Termin", "start": {"date": "2026-09-26"},
                                  "end": {"date": "2026-09-27"}}).execute()
    s = _sync(SAMPLE.replace("20.12.2026, 00:00 Uhr bis 23:59 Uhr\n", ""))
    assert s["deleted"] == 1
    assert own["id"] in svc.cal.events


def test_legacy_keys_are_migrated(svc):
    import gcal
    _sync(SAMPLE)
    # alte App-Version simulieren: Startzeit im Schlüssel
    for e in _managed(svc, "sperrtermin"):
        p = e["extendedProperties"]["private"]
        hhmm = e["start"]["dateTime"][11:16].replace(":", "")
        p["bwhv_sync_key"] = p["bwhv_sync_key"] + "-" + hhmm
    s = _sync(SAMPLE)
    assert (s["added"], s["deleted"], s["updated"]) == (0, 0, 0)
    keys = {e["extendedProperties"]["private"]["bwhv_sync_key"] for e in _managed(svc, "sperrtermin")}
    assert "sperrtermin-2026-09-26" in keys  # auf neues Format umgestellt


def test_bis_date_change_keeps_series_start(svc, monkeypatch):
    import gcal
    _sync(SAMPLE, bis=date(2026, 10, 31))
    start_before = _managed(svc, "generell")[0]["start"]["dateTime"][:10]
    monkeypatch.setattr(gcal, "today", lambda: date(2026, 10, 15))
    s = _sync(SAMPLE, bis=date(2026, 12, 31))
    assert s["updated"] == 4
    assert _managed(svc, "generell")[0]["start"]["dateTime"][:10] == start_before
    assert all("UNTIL=20261231" in e["recurrence"][0] for e in _managed(svc, "generell"))


def test_marker_gets_label(svc):
    _sync(SAMPLE)
    m = _managed(svc, "marker")[0]
    assert m["eventLabelId"] == "L1" and m["start"]["date"] == "2026-12-31"


# ------------------------------------------------------------------- API ---

def _pdf_bytes():
    path = os.environ.get("BWHV_SAMPLE_PDF", "/mnt/user-data/uploads/PMNY4_.pdf")
    if not os.path.exists(path):
        pytest.skip("Beispiel-PDF nicht vorhanden")
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def client(svc, monkeypatch):
    import app as appmod
    import gcal
    monkeypatch.setattr(gcal, "is_authenticated", lambda: True)
    monkeypatch.setattr(gcal, "has_client_secret", lambda: True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_api_preview_then_import(client, svc):
    data = {"pdf": (io.BytesIO(_pdf_bytes()), "tkJ79_.pdf"), "bis_date": "2026-12-31"}
    r = client.post("/api/preview", data=data, content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    assert len(r.json["plan"]["add"]) > 0
    assert svc.cal.events == {}  # Vorschau ändert nichts

    data = {"pdf": (io.BytesIO(_pdf_bytes()), "tkJ79_.pdf"), "bis_date": "2026-12-31"}
    r = client.post("/api/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    assert r.json["sync"]["hinzugefuegt"] > 0
    assert client.get("/api/history").json["history"][0]["bis_date"] == "2026-12-31"
    assert client.get("/api/status").json["suggested_bis_date"] == "2026-12-31"


def test_api_rejects_non_pdf(client):
    data = {"pdf": (io.BytesIO(b"hallo"), "x.pdf"), "bis_date": "2026-12-31"}
    r = client.post("/api/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 422 and "kein PDF" in r.json["error"]


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Vorschau" in r.data
