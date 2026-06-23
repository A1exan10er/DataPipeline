"""Unified configuration helpers for event center."""

from pathlib import Path

from dcs_sdk.config import get_config_path as _get_config_path
from dcs_sdk.config import get_section, legacy_config


def get_config_path() -> Path:
    return _get_config_path()


def load_config() -> dict:
    return legacy_config()


def load_rabbitmq_config() -> dict:
    return get_section("rabbitmq")


def load_redis_config() -> dict:
    cfg = get_section("event_redis")
    return {
        "ip": cfg.get("host"),
        "port": cfg.get("port"),
        "pwd": cfg.get("password"),
        "db": cfg.get("db"),
    }


def get_rabbitmq_url() -> str:
    config = load_rabbitmq_config()
    return config.get("url")


def get_exchange_name() -> str:
    config = load_rabbitmq_config()
    return config.get("exchange", "event_center.events")
