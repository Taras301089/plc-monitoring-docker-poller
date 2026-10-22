"""SCADA feature packages, separate from the shared PMM application."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .gwm import mount as mount_gwm

STATIC_DIR = Path(__file__).resolve().parent / "static"


def mount(app: FastAPI) -> None:
    """Mount the shared SCADA navigation and its individual layout screens."""
    app.mount("/scada/static", StaticFiles(directory=str(STATIC_DIR)), name="scada-static")
    mount_gwm(app)
