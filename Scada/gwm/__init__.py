"""GWM screen routes and static assets."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router

STATIC_DIR = Path(__file__).resolve().parent / "static"
SCREEN_URL = "/scada/gwm/gwm-scada.html"
ASSETS = ("gwm-scada.html", "gwm-scada.css", "gwm-scada.js", "gwm-mb-alarm-map.json")


def _redirect(target: str):
    async def redirect():
        return RedirectResponse(target, status_code=307)
    return redirect


def mount(app: FastAPI) -> None:
    """Register before the application's generic /static and filename routes."""
    app.include_router(router)
    for filename in ASSETS:
        target = f"/scada/gwm/{filename}"
        for previous in (f"/static/{filename}", f"/{filename}"):
            app.add_api_route(previous, _redirect(target), methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/scada/gwm/", _redirect(SCREEN_URL), methods=["GET", "HEAD"], include_in_schema=False)
    app.mount("/scada/gwm", StaticFiles(directory=str(STATIC_DIR)), name="gwm-scada")
