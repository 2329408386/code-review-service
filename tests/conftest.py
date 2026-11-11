from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.storage import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "log",
        admin_username="admin",
        admin_password="secret",
    )
    value.ensure_directories()
    return value


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings.database_path)
