"""Pytest fixtures: fresh in-memory DB for every test."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Ensure the repo root is on sys.path so `import medic_aid_bot` works regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the global connection at a fresh DB file per test."""
    from medic_aid_bot import config, db

    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))

    conn = db.connect(str(db_file))
    db.init_schema(conn)
    db.set_conn(conn)

    yield conn

    conn.close()
    db.set_conn(None)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
