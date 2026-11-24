"""Tests für den Passwortschutz der Oberfläche."""
import os
import sys
import tempfile

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="untis-test-"))
for k, v in {"UNTIS_SERVER": "x", "UNTIS_SCHOOL": "s", "UNTIS_USER": "u", "UNTIS_PASSWORD": "p"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "geheim")
    appmod._fails.clear()
    return appmod.app.test_client()


def test_ohne_passwort_offen(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    c = appmod.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/api/status").get_json()["password_enabled"] is False
    assert c.get("/login").status_code == 302          # Login-Seite leitet weiter


def test_passwort_verlangt(locked):
    r = locked.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    for method, url in (("get", "/api/status"), ("post", "/api/sync"),
                        ("post", "/api/exclusions"), ("post", "/api/settings"),
                        ("post", "/api/auth/disconnect")):
        assert getattr(locked, method)(url).status_code == 401, url
    # öffentlich bleiben
    assert locked.get("/healthz").status_code == 200
    assert locked.get("/manifest.webmanifest").status_code == 200
    assert locked.get("/login").status_code == 200


def test_untis_passwort_ist_nicht_app_passwort(locked):
    # UNTIS_PASSWORD (WebUntis-Login) darf nicht als App-Passwort funktionieren
    assert locked.post("/login", data={"password": config.UNTIS_PASSWORD}).status_code == 401


def test_login_und_logout(locked):
    r = locked.post("/login", data={"password": "falsch"})
    assert r.status_code == 401 and "Falsches Passwort" in r.get_data(as_text=True)
    r = locked.post("/login", data={"password": "geheim", "next": "/"})
    assert r.status_code == 302
    st = locked.get("/api/status")
    assert st.status_code == 200 and st.get_json()["password_enabled"] is True
    assert locked.post("/logout").status_code == 302
    assert locked.get("/api/status").status_code == 401


def test_kein_offener_redirect(locked):
    r = locked.post("/login", data={"password": "geheim", "next": "//evil.example/"})
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_sperre_nach_fehlversuchen(locked):
    for _ in range(5):
        locked.post("/login", data={"password": "falsch"})
    r = locked.post("/login", data={"password": "geheim"})
    assert r.status_code == 401 and "Fehlversuche" in r.get_data(as_text=True)


def test_secret_key_bleibt_gleich():
    assert config.get_secret_key() == config.get_secret_key() == appmod.app.secret_key
    assert os.path.exists(config.SECRET_KEY_PATH)
