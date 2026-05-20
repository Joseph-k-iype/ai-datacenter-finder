"""Centralized config: env-driven Pydantic settings + YAML pipeline file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-driven settings. Loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "dc_india"
    pg_user: str = "dc"
    pg_password: str = "dc_dev_pw_change_me"
    pg_schema: str = "dc_india"

    # GEE
    gee_service_account_json: str | None = None
    gee_project: str | None = None

    # App
    log_level: str = "INFO"
    log_format: str = "json"
    pipeline_config: str = "configs/pipeline.yml"
    weights_config: str = "configs/weights/default.yml"

    # Overpass
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_timeout: int = 180
    overpass_rate_limit_sec: float = 1.0
    # overpass-api.de's front-end rejects requests sent with the default
    # python-requests User-Agent (HTTP 406). Identify the client per the
    # OSM API etiquette: app name + contact.
    overpass_user_agent: str = (
        "ai-data-center/0.1 (+https://github.com/anthropics/claude-code)"
    )

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _replace_project(d: dict[str, Any], project: str) -> None:
    for k, v in d.items():
        if isinstance(v, str):
            d[k] = v.replace("{project}", project)
        elif isinstance(v, dict):
            _replace_project(v, project)

@lru_cache(maxsize=1)
def load_pipeline_config() -> dict[str, Any]:
    settings = get_settings()
    path = PROJECT_ROOT / settings.pipeline_config
    with path.open("r") as f:
        cfg: dict[str, Any] = yaml.safe_load(f)
    if settings.gee_project:
        _replace_project(cfg, settings.gee_project)
    return cfg


def load_yaml(relative_path: str) -> dict[str, Any]:
    """Load a YAML file relative to the project root."""
    path = PROJECT_ROOT / relative_path
    with path.open("r") as f:
        return yaml.safe_load(f)


def load_weights(weights_path: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    path = Path(weights_path) if weights_path else PROJECT_ROOT / settings.weights_config
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    with path.open("r") as f:
        return yaml.safe_load(f)


def load_exclusions() -> dict[str, Any]:
    cfg = load_pipeline_config()
    file = cfg.get("exclusions", {}).get("use_file", "configs/exclusions.yml")
    return load_yaml(file)


def load_sources() -> dict[str, Any]:
    return load_yaml("configs/sources.yml")
