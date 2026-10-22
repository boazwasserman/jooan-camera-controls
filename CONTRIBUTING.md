# Contributing

1. Keep examples and fixtures synthetic; never submit secrets, private
   addresses, device/account identifiers, logs, media, or raw vendor responses.
2. Preserve the domain `jooan_night_vision` and the guarded PTZ lifecycle.
3. Add deterministic tests for behavior changes, including failure and cleanup
   paths.
4. Use Python 3.14 and a repository-local virtual environment. Install
   `requirements-dev.txt`, then run the checks below before opening a pull
   request:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
.venv/bin/python -m ruff check custom_components tests tools
.venv/bin/python -m compileall -q custom_components
git diff --check
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
The pinned development dependencies are the reproducible qualification
environment; runtime minimum dependencies in the manifest may advance with
Home Assistant. The CI also runs the official Hassfest and HACS validators.
