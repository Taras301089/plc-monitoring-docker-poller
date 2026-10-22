SCADA screens

Open /#scada in PMM. The SCADA tab dropdown and the left navigation offer
GWM, Changan, Chery and Main Layout. GWM has a live station layout; the other
screens show empty states until their layouts and signal bindings are supplied.
The selected layout is remembered in this browser. The GWM frame is removed
when another PMM tab or SCADA layout is selected, stopping its background poll.

Scada/static/scada-navigation.js and .css provide the shared navigation.
The shared /static/theme.js reads PMM's existing "theme" preference and keeps
PMM, the embedded or standalone GWM screen, and the help page synchronized.
The theme changes appearance only; station mode colours retain their meaning.

GWM code is kept together in Scada/gwm:
  api.py                 Read-only live station API and PLC signal discovery.
  __init__.py            Route registration and compatibility redirects.
  static/                HTML, CSS, JavaScript and the verified partial alarm map.
  tests/                 Backend, signal logic and route regression tests.

The shared FastAPI application registers Scada.mount(app), including the GWM package.
Runtime dependencies (FastAPI and httpx) remain in fastapi-api/requirements.txt.
No PLC program, export, credentials or schema migration belongs in this package.
The alarm map's source path is relative to the external TIA_PROJECTS_ROOT archive.

Routes:
  /#scada                  SCADA tab in the PMM application.
  /scada/static/           Shared SCADA navigation assets.
  /scada/gwm/gwm-scada.html  GWM screen; /scada/gwm/ redirects here.
                           ?embedded=1 uses a compact header inside PMM.
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

Signal handover:
  GWM_missing_signals_20261022.txt lists the remaining CARID, alarm-message and
  equipment mapping gaps. FALSE is a valid value, not a missing signal.
