"""Task-to-device-category reference lookup for Phase 1 checks.

Credentials are intentionally read only from environment variables. Do not put
database passwords in config files or command-line arguments.
"""

from __future__ import annotations

import csv
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from scripts.pipeline.qa_config import config_value


CATEGORY_SINGLE_ARM = "single_arm"
CATEGORY_DUAL_ARM = "dual_arm"
CATEGORY_UMI = "umi"

DEFINED_CATEGORIES = {CATEGORY_SINGLE_ARM, CATEGORY_DUAL_ARM, CATEGORY_UMI}


def task_device_category(task_names: list[str]) -> dict[str, Any]:
    """Return the reference device category for the first matching task name."""
    mapping = _task_reference_mapping()
    for task_name in task_names:
        normalized = _normalize_name(task_name)
        if not normalized:
            continue
        if normalized in mapping:
            row = mapping[normalized]
            return {
                "category": row["category"],
                "defined": row["category"] in DEFINED_CATEGORIES,
                "matched_task_key": row["task_key"],
                "source": row["source"],
                "raw_compatible_device_types": row["raw"],
                "reference_available": True,
            }
    return {
        "category": "",
        "defined": False,
        "matched_task_key": "",
        "source": _reference_source_name(),
        "raw_compatible_device_types": None,
        "reference_available": bool(mapping),
    }


def robot_device_category(robot: str) -> str:
    """Classify an episode robot/source as single-arm, dual-arm, or UMI."""
    normalized = _normalize_robot_value(robot)
    if not normalized:
        return ""
    configured = config_value(["phase1_metadata", "robot_device_categories"], {})
    categories = configured if isinstance(configured, dict) else _default_robot_device_categories()
    for category, aliases in categories.items():
        if str(category) not in DEFINED_CATEGORIES or not isinstance(aliases, list):
            continue
        normalized_aliases = {_normalize_robot_value(str(alias)) for alias in aliases}
        if normalized in normalized_aliases:
            return str(category)
    return ""


def categories_compatible(task_category: str, robot_category: str) -> bool:
    """Return whether task and robot categories are compatible."""
    return task_category in DEFINED_CATEGORIES and task_category == robot_category


@lru_cache(maxsize=1)
def _task_reference_mapping() -> dict[str, dict[str, Any]]:
    file_mapping = _load_reference_file()
    if file_mapping:
        return file_mapping
    return _load_reference_database()


def _load_reference_file() -> dict[str, dict[str, Any]]:
    path_value = os.environ.get("QA_TASK_REFERENCE_FILE", "").strip()
    if not path_value:
        return {}
    path = Path(path_value)
    try:
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
        else:
            with path.open("r", encoding="utf-8", newline="") as file_obj:
                rows = list(csv.DictReader(file_obj))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(rows, list):
        return {}
    return _rows_to_mapping(rows, f"file:{path}")


def _load_reference_database() -> dict[str, dict[str, Any]]:
    host = os.environ.get("QA_TASK_DB_HOST", "").strip()
    database = os.environ.get("QA_TASK_DB_NAME", "").strip()
    user = os.environ.get("QA_TASK_DB_USER", "").strip()
    password = os.environ.get("QA_TASK_DB_PASSWORD", "")
    if not (host and database and user and password):
        return {}
    port = int(os.environ.get("QA_TASK_DB_PORT", "5432"))
    table = os.environ.get("QA_TASK_DB_TABLE", "public.tasks").strip()
    task_column = os.environ.get("QA_TASK_DB_TASK_COLUMN", "task_key").strip()
    device_column = os.environ.get("QA_TASK_DB_DEVICE_COLUMN", "compatible_device_types").strip()
    statement = f"SELECT {task_column}, {device_column} FROM {table}"
    try:
        rows = _query_postgres(host, port, database, user, password, statement)
    except Exception:
        return {}
    return _rows_to_mapping(rows, f"postgres:{host}/{database}.{table}")


def _query_postgres(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    statement: str,
) -> list[dict[str, Any]]:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        psycopg = None
    if psycopg is not None:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=database,
            user=user,
            password=password,
            connect_timeout=5,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(statement)
                return [
                    {"task_key": row[0], "compatible_device_types": row[1]}
                    for row in cursor.fetchall()
                ]
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError:
        return []
    with psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(statement)
            return [
                {"task_key": row[0], "compatible_device_types": row[1]}
                for row in cursor.fetchall()
            ]


def _rows_to_mapping(rows: list[Any], source: str) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_key = _row_value(row, "task_key")
        compatible_device_types = _row_value(row, "compatible_device_types")
        normalized_task = _normalize_name(str(task_key or ""))
        if not normalized_task:
            continue
        category = _category_from_device_types(compatible_device_types)
        mapping[normalized_task] = {
            "task_key": str(task_key),
            "category": category,
            "raw": _safe_json_value(compatible_device_types),
            "source": source,
        }
    return mapping


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _category_from_device_types(value: Any) -> str:
    values = _device_type_values(value)
    categories = {_normalize_device_type(item) for item in values}
    categories.discard("")
    if CATEGORY_UMI in categories:
        return CATEGORY_UMI
    if CATEGORY_DUAL_ARM in categories:
        return CATEGORY_DUAL_ARM
    if CATEGORY_SINGLE_ARM in categories:
        return CATEGORY_SINGLE_ARM
    return ""


def _device_type_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
        return [str(decoded)]
    return [str(value)]


def _normalize_device_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "single": CATEGORY_SINGLE_ARM,
        "single_arm": CATEGORY_SINGLE_ARM,
        "one_arm": CATEGORY_SINGLE_ARM,
        "单臂": CATEGORY_SINGLE_ARM,
        "dual": CATEGORY_DUAL_ARM,
        "dual_arm": CATEGORY_DUAL_ARM,
        "double_arm": CATEGORY_DUAL_ARM,
        "two_arm": CATEGORY_DUAL_ARM,
        "双臂": CATEGORY_DUAL_ARM,
        "umi": CATEGORY_UMI,
    }
    return aliases.get(normalized, "")


def _safe_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def _normalize_name(value: str) -> str:
    tokens = []
    current = []
    for char in value.strip().lower():
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return "_".join(tokens)


def _normalize_robot_value(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _default_robot_device_categories() -> dict[str, list[str]]:
    return {
        CATEGORY_SINGLE_ARM: [
            "ur",
            "ur5",
            "ur5e",
            "ur7e",
            "franka",
            "fr3",
            "fr3v2",
            "flexiv",
            "flexiv_rizon4",
        ],
        CATEGORY_DUAL_ARM: [
            "arx",
            "arx5",
            "aloha",
            "piper",
            "aloha_piper",
        ],
        CATEGORY_UMI: ["umi"],
    }


def _reference_source_name() -> str:
    if os.environ.get("QA_TASK_REFERENCE_FILE", "").strip():
        return "file"
    if os.environ.get("QA_TASK_DB_HOST", "").strip():
        return "postgres"
    return ""
