"""Pytest configuration: fixtures and environment setup.

Environment variables are set BEFORE any app module import so
config.settings captures test values (it reads the env once at import
time). Every fixture database is a fresh temp file — tests never touch
the real data/ directory.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate from the developer's real configuration before anything imports
# app.config. The repo root must be on sys.path for `import app.*` when
# pytest is invoked from inside tests/ as well.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault(
    "BND_DB_PATH", os.path.join(tempfile.gettempdir(), "bnd_uninit.db")
)
os.environ.setdefault(
    "BND_DOWNLOAD_DIR", os.path.join(tempfile.gettempdir(), "bnd_uninit_dl")
)
os.environ.setdefault(
    "BND_DATA_DIR", os.path.join(tempfile.gettempdir(), "bnd_uninit_data")
)
os.environ.setdefault("BND_SCHEDULER_INTERVAL_SECONDS", "3600")  # keep loops quiet
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """Fresh BND_* environment pointing into a per-test temp dir.

    Reloads app.config so settings picks the values up, then yields.
    """
    monkeypatch.setenv("BND_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BND_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("BND_DATA_DIR", str(tmp_path / "data"))
    import importlib

    import app.config

    importlib.reload(app.config)
    yield tmp_path
    importlib.reload(app.config)


@pytest.fixture()
def db(tmp_env):
    """A fresh Database on a per-test temp file."""
    from app.db import Database

    return Database(str(tmp_env / "test.db"))


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def app_client(tmp_env):
    """TestClient with routes wired; yields (client, db, manager).

    Reload order: dependencies first, app.main LAST (main captures
    `from . import routes` at import time).
    """
    import os

    fastapi_testclient = pytest.importorskip("fastapi.testclient")

    import app.config as config_mod
    import app.db as db_mod
    import app.downloader as dl_mod
    import app.makerworld as mw_mod
    import app.routes as routes_mod
    import app.scheduler as sched_mod

    for mod in (config_mod, db_mod, dl_mod, mw_mod, routes_mod, sched_mod):
        importlib.reload(mod)
    import app.main as main_mod

    importlib.reload(main_mod)

    from app.db import Database
    from app.downloader import DownloadManager
    from app.scheduler import SyncScheduler

    database = Database(os.path.join(str(tmp_env), "test.db"))
    manager = DownloadManager(database)
    scheduler = SyncScheduler(database, manager)
    routes_mod.init(database, manager, scheduler)

    with fastapi_testclient.TestClient(main_mod.app) as client:
        yield client, database, manager
