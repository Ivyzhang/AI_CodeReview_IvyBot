from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_app_id: str
    github_app_private_key_path: Path
    github_webhook_secret: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    database_path: Path = Path("review.sqlite3")
    max_patch_chars: int = Field(default=6000, gt=0)
    max_input_chars: int = Field(default=60_000, gt=0)
    max_comments: int = Field(default=20, gt=0, le=100)
    model_timeout_seconds: float = Field(default=120, gt=0)
    worker_poll_seconds: float = Field(default=0.5, gt=0)
    stale_task_minutes: int = Field(default=10, gt=0)
    installation_daily_task_limit: int = Field(default=200, gt=0)
