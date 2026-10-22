SCADA screens

GWM code is kept together in Scada/gwm:
  api.py                 Read-only live station API and PLC signal discovery.
  __init__.py            Route registration and compatibility redirects.
  static/                HTML, CSS, JavaScript and the verified partial alarm map.
  tests/                 Backend, signal logic and route regression tests.

The shared FastAPI application registers this package with mount(app).
Runtime dependencies (FastAPI and httpx) remain in fastapi-api/requirements.txt.
No PLC program, export, credentials or schema migration belongs in this package.
The alarm map's source path is relative to the external TIA_PROJECTS_ROOT archive.

Routes:
  /scada/gwm/gwm-scada.html  GWM screen; /scada/gwm/ redirects here.
  /api/scada/gwm            Live state; optional ?station=MB200 adds details.
  Earlier /static/gwm-* and /gwm-* asset links redirect to the new location.

Build from the repository root:
  docker compose build fastapi-api
  docker compose up -d --no-deps fastapi-api
The Dockerfile-specific allowlist includes API and SCADA runtime sources only.
Development compose uses the same root context and Dockerfile.

Tests from the repository root (no live PLC required):
  python -m unittest discover -s Scada/gwm/tests -p test_gwm_scada.py -v
  python -m pytest --noconftest Scada/gwm/tests -q
Isolated integration subset, also without database setup:
  python -m pytest --noconftest -c tests/pytest.ini Scada/gwm/tests tests/test_dockerfile_modules.py tests/test_opc_browse.py -q
Node.js is required for JavaScript behavior tests; pytest and test dependencies
are listed in tests/requirements-test.txt. The ordinary tests/pytest.ini also
includes this folder, so run-tests.ps1 continues to collect these tests.
The --noconftest option is intentional for isolated runs: the shared tests/
conftest.py creates a test database during pytest configuration even when the
selected tests themselves need no database. Full run-tests.ps1 uses its normal
disposable database workflow instead.
