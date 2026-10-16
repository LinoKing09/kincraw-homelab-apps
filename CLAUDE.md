# Hinweise für Claude

- Sprache: Deutsch (Code-Kommentare, Doku, Commit-Messages).
- Jeder Push auf `main` wird automatisch gebaut und landet innerhalb von ~15 Minuten
  auf docker1 (Produktion). Vor dem Push: `pytest -q bwhv-sync/tests/` und
  `python -m py_compile untis-sync/*.py` lokal laufen lassen.
- Niemals Zugangsdaten oder Laufzeitdaten committen (`data/`, `.env`,
  `client_secret*.json`, `token.json`, `secret_key`).
- Image-Namen in GHCR sind kleingeschrieben: `ghcr.io/linoking09/<app>`.
- Neue App: eigener Ordner mit `Dockerfile`, `.dockerignore`, `README.md`;
  in `.github/workflows/build.yml` zur Matrix hinzufügen; in `deploy/compose.yaml`
  UND im eingebetteten Block in `deploy/setup-docker1.sh` eintragen. Änderungen an
  der Server-`compose.yaml` kommen NICHT automatisch auf docker1 – dem Nutzer sagen,
  was er dort anpassen muss.
- Datenformate in `data/` (token.json, history.json, state.json, exclusions.json)
  rückwärtskompatibel halten, da die bestehenden Dateien auf dem Server weiterverwendet werden.
