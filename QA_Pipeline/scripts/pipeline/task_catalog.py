"""Task-duration catalog loading and per-run snapshot support."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from scripts.pipeline.qa_config import config_value


QA_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FALLBACK_PATH = QA_ROOT / "configs" / "task_time_ranges_fallback.json"
SNAPSHOT_FILENAME = "task_time_ranges_snapshot.json"


@dataclass(frozen=True)
class TaskCatalog:
    items: dict[str, dict[str, Any]]
    source: str
    source_url: str
    sha256: str
    fetched_at: str
    error: str = ""


def prepare_task_catalog(
    output_dir: Path,
    opener: Callable[..., Any] = urlopen,
) -> TaskCatalog:
    """Load the task catalog once and write the effective per-run snapshot."""
    settings = config_value(["task_catalog"], {})
    if not isinstance(settings, dict):
        raise RuntimeError("task_catalog configuration must be a JSON object")
    enabled = bool(settings.get("enabled", True))
    url = str(settings.get("url") or "").strip()
    timeout = float(settings.get("timeout_seconds", 5.0))
    fallback_path = _config_path(
        settings.get("fallback_path", "configs/task_time_ranges_fallback.json")
    )
    fetched_at = datetime.now().astimezone().isoformat()
    payload: dict[str, Any] | None = None
    source = "disabled"
    error = ""

    if enabled and url:
        try:
            with opener(url, timeout=timeout) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            _validate_payload(payload)
            source = "live"
        except Exception as exc:  # network and schema errors share fallback behavior
            payload = None
            error = f"Live task catalog unavailable: {exc}"

    if payload is None and enabled:
        try:
            payload = json.loads(fallback_path.read_text(encoding="utf-8"))
            _validate_payload(payload)
            source = "fallback"
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            payload = None
            fallback_error = f"Fallback task catalog unavailable: {exc}"
            error = f"{error}; {fallback_error}" if error else fallback_error

    if payload is None:
        payload = {"items": {}, "total": 0}
        source = "default"

    items = _normalized_items(payload["items"])
    canonical = json.dumps(
        {"items": items, "total": len(items)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    catalog = TaskCatalog(
        items=items,
        source=source,
        source_url=url,
        sha256=digest,
        fetched_at=fetched_at,
        error=error,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "source": source,
        "source_url": url,
        "fetched_at": fetched_at,
        "sha256": digest,
        "error": error,
        "total": len(items),
        "items": items,
    }
    (output_dir / SNAPSHOT_FILENAME).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Task duration catalog: source={source}, tasks={len(items)}, "
        f"snapshot={output_dir / SNAPSHOT_FILENAME}",
        flush=True,
    )
    if error:
        print(f"Task duration catalog diagnostic: {error}", flush=True)
    return catalog


def resolve_task_policy(
    task_key: str,
    metadata: dict[str, Any],
    catalog: TaskCatalog | None,
) -> dict[str, Any]:
    """Resolve demo status and maximum duration using documented precedence."""
    settings = config_value(["phase2_duration", "training_duration_limit"], {})
    if not isinstance(settings, dict):
        raise RuntimeError("phase2_duration.training_duration_limit must be a JSON object")
    default_max = float(settings.get("default_max_duration_seconds", 120.0))
    overrides = settings.get("task_overrides", {})
    if not isinstance(overrides, dict):
        raise RuntimeError("training_duration_limit.task_overrides must be a JSON object")

    exact_override = overrides.get(task_key)
    if exact_override is not None:
        if not isinstance(exact_override, dict):
            raise RuntimeError(f"Task duration override for {task_key!r} must be a JSON object")
        return _policy_from_values(exact_override, default_max, "config_override", False)

    if catalog is not None and task_key in catalog.items:
        return _policy_from_values(
            catalog.items[task_key], default_max, f"task_catalog_{catalog.source}", False
        )

    metadata_policy = _metadata_policy(metadata)
    if metadata_policy is not None:
        return _policy_from_values(metadata_policy, default_max, "episode_metadata", True)

    return {
        "is_demo": False,
        "max_duration_seconds": default_max,
        "source": "unknown_task_default",
        "catalog_missing": True,
    }


def _metadata_policy(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(metadata.get("is_demo"), bool):
        return {
            "is_demo": metadata["is_demo"],
            "max_duration_seconds": metadata.get("max_duration_seconds"),
        }
    stage = str(metadata.get("training_stage") or "").strip().lower()
    if stage in {"demo", "post_training", "post-training"}:
        return {"is_demo": True, "max_duration_seconds": metadata.get("max_duration_seconds")}
    if stage in {"pretraining", "pre_training", "pre-training"}:
        return {"is_demo": False, "max_duration_seconds": metadata.get("max_duration_seconds")}
    return None


def _policy_from_values(
    values: dict[str, Any],
    default_max: float,
    source: str,
    catalog_missing: bool,
) -> dict[str, Any]:
    is_demo = values.get("is_demo", False)
    if not isinstance(is_demo, bool):
        raise RuntimeError(f"Task duration policy {source} has non-boolean is_demo")
    maximum = values.get("max_duration_seconds")
    if maximum is None:
        maximum = default_max
    try:
        maximum = float(maximum)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Task duration policy {source} has invalid maximum") from exc
    if maximum <= 0:
        raise RuntimeError(f"Task duration policy {source} maximum must be positive")
    return {
        "is_demo": is_demo,
        "max_duration_seconds": maximum,
        "source": source,
        "catalog_missing": catalog_missing,
    }


def _validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), dict):
        raise RuntimeError("Task catalog must contain an items JSON object")
    for task_key, values in payload["items"].items():
        if not isinstance(task_key, str) or not task_key.strip() or not isinstance(values, dict):
            raise RuntimeError("Task catalog entries require non-empty task keys and JSON objects")
        if not isinstance(values.get("is_demo"), bool):
            raise RuntimeError(f"Task catalog entry {task_key!r} requires boolean is_demo")
        maximum = values.get("max_duration_seconds")
        if maximum is not None and (
            not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or maximum <= 0
        ):
            raise RuntimeError(
                f"Task catalog entry {task_key!r} has invalid max_duration_seconds"
            )


def _normalized_items(items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for task_key, values in sorted(items.items()):
        item = {"is_demo": bool(values["is_demo"])}
        maximum = values.get("max_duration_seconds")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
            item["max_duration_seconds"] = float(maximum)
        minimum = values.get("min_duration_seconds")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            item["min_duration_seconds"] = float(minimum)
        normalized[task_key] = item
    return normalized


def _config_path(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = QA_ROOT / path
    return path.resolve(strict=False)
