"""Shared configuration loader used by server, collector, workflow and consumers."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

DCS_CONFIG_ENV = "DCS_CONFIG_FILE"
DCS_ENV_ENV = "DCS_ENV"
DEFAULT_CONFIG_NAME = "dcs_config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "use_deploy": True,
    "auth": {
        "secret_key": "XINZHIDCS",
        "algorithm": "HS256",
        "access_token_expire_minutes": 3600,
    },
    "server_database": {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "datacollect",
        "password": "datacollect_dev",
        "database": "data_collector_dev",
    },
    "event_database": {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "datacollect",
        "password": "datacollect_dev",
        "database": "event_center",
    },
    "redis": {
        "host": "127.0.0.1",
        "port": 6379,
        "password": "",
        "db": 1,
    },
    "rabbitmq": {
        "url": "",
        "exchange": "event_center.events",
    },
    "temporal": {
        "target_host": "127.0.0.1:7233",
        "task_queue_env": "TEMPORAL_TASK_QUEUE",
    },
    "nas": {
        "host": "192.168.50.2",
        "port": 22,
        "user": "xinzhi",
        "password": "XZ12345678fvl",
        "module": "database",
        "base_path": "/volume1/database",
        "staging_path": "/volume1/database/staging",
        "verified_path": "/volume1/database/verified",
        "export_path": "/volume1/database/exports",
        "upload_path": "/volume1/database/uploads",
        "sop_path": "/volume1/database/sop",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 7777,
        "api_base_url": "http://127.0.0.1:7777/api/v1",
    },
    "collector": {
        "host": "0.0.0.0",
        "port": 9527,
        "robot": "",
        "data_root": "./data",
        "collector_device_type": "unknown",
        "fps": 30,
        "episode_duration": 300,
        "reset_duration": 10,
        "num_episodes": 50,
    },
    "environments": {},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def get_config_path() -> Path:
    configured = os.getenv(DCS_CONFIG_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return project_root() / DEFAULT_CONFIG_NAME


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve() if path else get_config_path()
    if not config_path.exists():
        settings = deepcopy(DEFAULT_CONFIG)
        return _apply_environment(settings)
    with config_path.open(encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError(f"Config root must be an object: {config_path}")
    settings = _deep_merge(DEFAULT_CONFIG, loaded)
    return _apply_environment(settings)


def _apply_environment(settings: dict[str, Any]) -> dict[str, Any]:
    env_name = os.getenv(DCS_ENV_ENV, "").strip()
    if not env_name:
        if "active_environment" in settings:
            env_name = str(settings.get("active_environment", "")).strip()
        else:
            env_name = "deploy" if bool(settings.get("use_deploy", False)) else "local"
    environments = settings.get("environments", {})
    if not env_name or not isinstance(environments, dict):
        return settings

    profile = environments.get(env_name)
    if profile is None:
        # 配置文件不存在或未定义该环境时，直接使用默认值
        if not environments:
            return settings
        available = ", ".join(sorted(str(k) for k in environments.keys()))
        raise ValueError(
            f"Unknown DCS environment '{env_name}'. Available environments: {available}"
        )
    if not isinstance(profile, dict):
        raise ValueError(f"Environment profile '{env_name}' must be an object")

    merged = _deep_merge(settings, profile)
    merged["active_environment"] = env_name
    merged["use_deploy"] = env_name == "deploy"
    merged.pop("environments", None)
    return merged


@lru_cache(maxsize=1)
def get_settings() -> dict[str, Any]:
    return load_settings()


def get_section(name: str) -> dict[str, Any]:
    section = get_settings().get(name, {})
    return deepcopy(section) if isinstance(section, dict) else {}


def get_environment() -> str:
    return str(get_settings().get("active_environment", ""))


def database_section(name: str) -> dict[str, Any]:
    cfg = get_section(name)
    if cfg:
        return cfg
    return get_section("database")


def database_url(async_driver: bool = False, section: str = "server_database") -> str:
    cfg = database_section(section)
    user = quote_plus(str(cfg.get("user", "")))
    password = quote_plus(str(cfg.get("password", "")))
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 5432))
    database = cfg.get("database", "event_center")
    scheme = "postgresql+asyncpg" if async_driver else "postgresql"
    return f"{scheme}://{user}:{password}@{host}:{port}/{database}"


def redis_url() -> str:
    cfg = get_section("redis")
    password = str(cfg.get("password", "") or "")
    auth = f":{quote_plus(password)}@" if password else ""
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 6379))
    db = int(cfg.get("db", 0))
    return f"redis://{auth}{host}:{port}/{db}"


def legacy_config() -> dict[str, Any]:
    """Return the old config.json shape for compatibility during migration."""
    settings = get_settings()
    auth = settings["auth"]
    db = database_section("event_database")
    redis = settings["redis"]
    return {
        "secret_key": auth.get("secret_key", ""),
        "algorithm": auth.get("algorithm", "HS256"),
        "access_token_expire_minutes": auth.get("access_token_expire_minutes", 3600),
        "db": {
            "ip": db.get("host", "127.0.0.1"),
            "port": db.get("port", 5432),
            "user": db.get("user", "postgres"),
            "pwd": db.get("password", ""),
            "database": db.get("database", "event_center"),
        },
        "redis": {
            "ip": redis.get("host", "127.0.0.1"),
            "port": redis.get("port", 6379),
            "pwd": redis.get("password", ""),
            "db": redis.get("db", 0),
        },
        "rabbitmq": deepcopy(settings["rabbitmq"]),
    }
