from __future__ import annotations

from collections.abc import Iterator

import pytest

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.db.session import create_all_tables, session_scope


@pytest.fixture()
def sqlite_settings(tmp_path, monkeypatch) -> Settings:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    get_settings.cache_clear()
    settings = get_settings()
    create_all_tables(settings)
    return settings


@pytest.fixture()
def repo(sqlite_settings: Settings) -> Iterator[Repository]:
    with session_scope(sqlite_settings) as session:
        yield Repository(session)
