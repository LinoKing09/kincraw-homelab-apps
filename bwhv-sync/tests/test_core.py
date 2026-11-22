# -*- coding: utf-8 -*-
"""Tests ohne Google-Zugang: Parser, Abgleich-Logik (mit Fake-Kalender), API-Routen."""
import copy
import io
import itertools
import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BWHV_DISABLE_SCHEDULER", "1")   # keine Hintergrund-Threads im Test

import history  # noqa: E402  (erst nach sys.path.insert importierbar)

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


# ------------------------------------------------- ältere PDF erkennen ---

PDF_STUB = b"%PDF-1.4 stub"


@pytest.fixture
def fake_pdf(monkeypatch):
    """PDF-Parsing durch SAMPLE-Text ersetzen; .text änderbar pro Test."""
    import app as appmod
    from parser import parse_text
    state = {"text": SAMPLE}
    monkeypatch.setattr(appmod, "parse_freiwunschliste", lambda path: parse_text(state["text"]))
    return state


def _post(client, url, **extra):
    data = {"pdf": (io.BytesIO(PDF_STUB), "fwl.pdf"), "bis_date": "2026-12-31", **extra}
    return client.post(url, data=data, content_type="multipart/form-data")


def test_older_pdf_warns_and_needs_force(client, svc, fake_pdf):
    assert _post(client, "/api/import").status_code == 200          # Stand 14.08.2026 00:19
    fake_pdf["text"] = SAMPLE.replace("14.08.2026 00:19", "01.08.2026 10:00")

    r = _post(client, "/api/preview")
    assert r.status_code == 200
    assert r.json["older_pdf"] == {"pdf": "01.08.2026 10:00", "newest": "14.08.2026 00:19"}

    r = _post(client, "/api/import")
    assert r.status_code == 409 and r.json["older_pdf"]
    assert len(client.get("/api/history").json["history"]) == 1     # nichts importiert

    assert _post(client, "/api/import", force="1").status_code == 200
    # gleicher oder neuerer Stand ist kein Problem
    fake_pdf["text"] = SAMPLE
    assert _post(client, "/api/preview").json["older_pdf"] is None


# ------------------------------------------------------------- Teilen ---

def test_share_target_roundtrip(client):
    import json as _json
    r = client.post("/share", data={"pdf": (io.BytesIO(PDF_STUB), "tkJ79_.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 303 and "shared=" in r.headers["Location"]
    token = r.headers["Location"].split("shared=")[1]
    f = client.get(f"/api/shared/{token}")
    assert f.status_code == 200 and f.data == PDF_STUB and f.headers["X-Filename"] == "tkJ79_.pdf"

    r = client.post("/share", data={"pdf": (io.BytesIO(b"kein pdf"), "x.pdf")},
                    content_type="multipart/form-data")
    assert "shared_error=nopdf" in r.headers["Location"]
    assert client.get("/api/shared/../../etc/passwd").status_code == 404
    assert client.get("/api/shared/" + "0" * 32).status_code == 404

    m = _json.loads(client.get("/manifest.json").data)
    assert m["share_target"]["action"] == "/share"
    assert m["share_target"]["params"]["files"][0]["name"] == "pdf"


# ---------------------------------------------------------- Erinnerung ---

@pytest.fixture
def gotify(monkeypatch):
    import notify
    sent = []

    class R:
        status_code = 200

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "key": headers["X-Gotify-Key"]})
        r = R(); r.status_code = gotify_status["code"]; return r
    gotify_status = {"code": 200}
    monkeypatch.setattr(notify.requests, "post", fake_post)
    notify.save_settings({"url": "http://gotify.local:8080/", "token": "Aabc", "days": "7, 2"})
    return sent, gotify_status


def test_notify_settings_validation():
    import notify
    with pytest.raises(notify.NotifyError):
        notify.save_settings({"url": "gotify.local"})
    with pytest.raises(notify.NotifyError):
        notify.save_settings({"url": "http://g", "token": "t", "days": "sieben"})
    s = notify.save_settings({"url": "http://g/", "token": "t", "days": "2;7;7"})
    assert s == {"configured": True, "url": "http://g", "token_set": True, "days": [7, 2],
                 "from_hour": notify.SEND_FROM_HOUR}
    # leeres Token behält das alte, leere Adresse schaltet aus
    assert notify.save_settings({"url": "http://h", "token": ""})["token_set"] is True
    assert notify.save_settings({"url": ""})["configured"] is False


def test_notify_check_thresholds(gotify):
    import config
    import notify
    sent, _ = gotify
    history.add({"bis_date": "2026-12-31"})
    at = lambda d, h=18: datetime(2026, 12, d, h, 0, tzinfo=config.TZ)  # noqa: E731

    assert notify.check(at(20)) is None                     # 11 Tage vorher: noch nichts
    assert notify.check(at(25, 9)) is None                  # vor 17 Uhr: nichts
    assert "6 Tage" in notify.check(at(25))                 # 7-Tage-Schwelle überschritten
    assert notify.check(at(26)) is None                     # nicht doppelt
    assert "bis morgen" in notify.check(at(30))             # 2-Tage-Schwelle
    assert notify.check(at(31)) is None
    assert len(sent) == 2 and sent[0]["url"] == "http://gotify.local:8080/message"
    assert sent[0]["key"] == "Aabc"

    history.add({"bis_date": "2027-01-31"})                 # neuer Import setzt zurück
    assert notify.check(at(31)) is None


def test_notify_skipped_thresholds_send_once(gotify):
    import config
    import notify
    sent, _ = gotify
    history.add({"bis_date": "2026-12-31"})
    assert "nur noch bis heute" in notify.check(datetime(2026, 12, 31, 20, tzinfo=config.TZ))
    assert len(sent) == 1                                   # 7 und 2 zusammen -> eine Nachricht


def test_notify_bad_token_message(gotify, client):
    _, st = gotify
    st["code"] = 401
    r = client.post("/api/notify/test")
    assert r.status_code == 422 and "App-Token" in r.json["error"]
    assert client.get("/api/status").json["notify"]["configured"] is True
