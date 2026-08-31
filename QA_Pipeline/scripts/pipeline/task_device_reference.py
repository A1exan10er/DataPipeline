"""Task-to-device-category reference lookup for Phase 1 checks.

Credentials are intentionally read only from environment variables. Do not put
database passwords in config files or command-line arguments.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Collection

from scripts.pipeline.qa_config import config_value


CATEGORY_SINGLE_ARM = "single_arm"
CATEGORY_DUAL_ARM = "dual_arm"
CATEGORY_UMI = "umi"

DEFINED_CATEGORIES = {CATEGORY_SINGLE_ARM, CATEGORY_DUAL_ARM, CATEGORY_UMI}


@dataclass(frozen=True)
class TaskReferenceSnapshot:
    """One cached view of the configured task compatibility catalog."""

    mapping: dict[str, dict[str, Any]]
    source: str
    available: bool
    error: str = ""


def task_device_category(task_names: list[str]) -> dict[str, Any]:
    """Return all reference device categories for the first matching task name."""
    snapshot = _task_reference_snapshot()
    mapping = snapshot.mapping
    for task_name in task_names:
        normalized = _normalize_name(task_name)
        if not normalized:
            continue
        if normalized in mapping:
            row = mapping[normalized]
            categories = list(row["categories"])
            return {
                # Keep the singular field only when it is unambiguous. Older
                # result readers can continue to consume single-category rows
                # without accidentally treating a multi-category task as one.
                "category": categories[0] if len(categories) == 1 else "",
                "categories": categories,
                "defined": bool(categories),
                "matched_task_key": row["task_key"],
                "source": row["source"],
                "raw_compatible_device_types": row["raw"],
                "reference_available": snapshot.available,
                "reference_error": snapshot.error,
            }
    return {
        "category": "",
        "categories": [],
        "defined": False,
        "matched_task_key": "",
        "source": snapshot.source,
        "raw_compatible_device_types": None,
        "reference_available": snapshot.available,
        "reference_error": snapshot.error,
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


def categories_compatible(
    task_categories: str | Collection[str], robot_category: str
) -> bool:
    """Return whether the robot belongs to any task-compatible category."""
    if isinstance(task_categories, str):
        allowed = {task_categories}
    else:
        allowed = {str(category) for category in task_categories}
    return robot_category in DEFINED_CATEGORIES and robot_category in allowed


def _task_reference_mapping() -> dict[str, dict[str, Any]]:
    """Compatibility wrapper for callers that only need the mapping."""
    return _task_reference_snapshot().mapping


@lru_cache(maxsize=1)
def _task_reference_snapshot() -> TaskReferenceSnapshot:
    file_snapshot = _load_reference_file()
    if file_snapshot.available:
        return file_snapshot

    database_snapshot = _load_reference_database()
    if database_snapshot.available:
        return database_snapshot

    failed_snapshots = [
        snapshot
        for snapshot in (file_snapshot, database_snapshot)
        if snapshot.error
    ]
    errors = [snapshot.error for snapshot in failed_snapshots]
    source = ";".join(
        snapshot.source for snapshot in failed_snapshots if snapshot.source
    )
    return TaskReferenceSnapshot({}, source, False, "; ".join(errors))


def _load_reference_file() -> TaskReferenceSnapshot:
    path_value = os.environ.get("QA_TASK_REFERENCE_FILE", "").strip()
    if not path_value:
        return TaskReferenceSnapshot({}, "", False)
    path = Path(path_value)
    source = f"file:{path}"
    try:
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
        else:
            with path.open("r", encoding="utf-8", newline="") as file_obj:
                rows = list(csv.DictReader(file_obj))
    except (OSError, json.JSONDecodeError) as exc:
        return TaskReferenceSnapshot(
            {}, source, False, f"Task reference file could not be loaded: {exc}"
        )
    if not isinstance(rows, list):
        return TaskReferenceSnapshot(
            {}, source, False, "Task reference file must contain a list of task rows."
        )
    mapping = _rows_to_mapping(rows, source)
    if not mapping:
        return TaskReferenceSnapshot(
            {}, source, False, "Task reference file contained no usable task rows."
        )
    return TaskReferenceSnapshot(mapping, source, True)


def _load_reference_database() -> TaskReferenceSnapshot:
    host = os.environ.get("QA_TASK_DB_HOST", "").strip()
    database = os.environ.get("QA_TASK_DB_NAME", "").strip()
    user = os.environ.get("QA_TASK_DB_USER", "").strip()
    password = os.environ.get("QA_TASK_DB_PASSWORD", "")
    table = os.environ.get("QA_TASK_DB_TABLE", "public.tasks").strip()
    source = f"postgres:{host}/{database}.{table}" if host and database else "postgres"
    if not (host and database and user and password):
        return TaskReferenceSnapshot(
            {}, source, False, "PostgreSQL task reference is not fully configured."
        )
    try:
        port = int(os.environ.get("QA_TASK_DB_PORT", "5432"))
    except ValueError as exc:
        return TaskReferenceSnapshot(
            {}, source, False, f"PostgreSQL task reference port is invalid: {exc}"
        )
    task_column = os.environ.get("QA_TASK_DB_TASK_COLUMN", "task_key").strip()
    device_column = os.environ.get("QA_TASK_DB_DEVICE_COLUMN", "compatible_device_types").strip()
    statement = f"SELECT {task_column}, {device_column} FROM {table}"
    try:
        rows = _query_postgres(host, port, database, user, password, statement)
    except Exception as exc:
        return TaskReferenceSnapshot(
            {},
            source,
            False,
            "PostgreSQL task reference query failed: "
            + _redacted_error_message(exc, password),
        )
    mapping = _rows_to_mapping(rows, source)
    if not mapping:
        return TaskReferenceSnapshot(
            {}, source, False, "PostgreSQL task reference query returned no usable task rows."
        )
    return TaskReferenceSnapshot(mapping, source, True)


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
        raise RuntimeError("neither psycopg nor psycopg2 is installed")
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
        categories = _categories_from_device_types(compatible_device_types)
        mapping[normalized_task] = {
            "task_key": str(task_key),
            "categories": categories,
            "category": categories[0] if len(categories) == 1 else "",
            "raw": _safe_json_value(compatible_device_types),
            "source": source,
        }
    return mapping


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _redacted_error_message(exc: Exception, secret: str) -> str:
    message = str(exc)
    return message.replace(secret, "<redacted>") if secret else message


def _categories_from_device_types(value: Any) -> list[str]:
    """Normalize every recognized compatible device type, preserving order."""
    values = _device_type_values(value)
    categories: list[str] = []
    for item in values:
        category = _normalize_device_type(item)
        if category and category not in categories:
            categories.append(category)
    return categories


def _category_from_device_types(value: Any) -> str:
    """Return a category only when the compatible type is unambiguous."""
    categories = _categories_from_device_types(value)
    return categories[0] if len(categories) == 1 else ""


def _device_type_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
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
    if isinstance(value, set):
        return sorted(value, key=str)
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
