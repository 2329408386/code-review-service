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
        codehub_private_token="",
        codehub_app_code="",
        comment_rate_interval_seconds=5.0,
        codex_timeout_seconds=5.0,
        git_command_timeout_seconds=10.0,
        allow_local_repository_paths=True,
    )
    value.ensure_directories()
    return value


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings.database_path)
