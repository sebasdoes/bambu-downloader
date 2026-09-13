"""Pytest configuration: fixtures and environment setup.

Environment variables are set BEFORE any app module import so
config.settings captures test values (it reads the env once at import
time). Every fixture database is a fresh temp file — tests never touch
the real data/ directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate from the developer's real configuration before anything imports
# app.config. The repo root must be on sys.path for `import app.*` when
# pytest is invoked from inside tests/ as well.
REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("BND_DB_PATH", os.path.join(tempfile.gettempdir(), "bnd_uninit.db"))
os.environ.setdefault("BND_DOWNLOAD_DIR", os.path.join(tempfile.gettempdir(), "bnd_uninit_dl"))
os.environ.setdefault("BND_DATA_DIR", os.path.join(tempfile.gettempdir(), "bnd_uninit_data"))
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