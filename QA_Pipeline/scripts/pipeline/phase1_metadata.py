"""Phase 1 structure and metadata checks."""

import hashlib
import json
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable

from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    decide_status,
    first_present,
    is_positive_number,
    load_metadata,
    save_episode_state,
    save_findings,
)
from scripts.pipeline.qa_config import config_value


PHASE_NUMBER = 1
CHECKSUM_MANIFEST = ".checksum_manifest"
MAX_DETAIL_PATHS_DEFAULT = 50


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run Phase 1 structure and metadata checks for each unfinished episode."""
    if workers > 1:
        return _run_phase_parallel(states, db_path, progress_callback, workers)
    processed_count = 0
    total_count = len(states)
    for state in states:
        if PHASE_NUMBER in state.phases_completed:
            processed_count += 1
            if progress_callback:
                progress_callback(processed_count, total_count)
            continue
        new_findings = _episode_findings(state)
        _record_metrics(state)
        _finish_state(state, db_path, new_findings)
        processed_count += 1
        if progress_callback:
            progress_callback(processed_count, total_count)
    return states


def _process_episode_worker(
    episode_path_str: str,
) -> tuple[str, list[dict], dict]:
    """Worker function for multiprocessing. Must be module-level for pickling.

    Args:
        episode_path_str: String path to the episode directory.

    Returns:
        Tuple of (episode_path_str, findings_as_dicts, metrics_dict)
        findings_as_dicts: list of Finding fields as plain dicts so they
            can be pickled across process boundaries.
    """
    from pathlib import Path

    episode_path = Path(episode_path_str)

    # Build a minimal temporary state for the checks. No db_path is needed in
    # the worker because results are returned to the main process.
    from scripts.pipeline.qa_core import EpisodeState
    import datetime

    state = EpisodeState(
        episode_path=episode_path,
        task="",
        date="",
        operator="",
        robot="",
        controller="",
        metadata={},
        phases_completed=[],
        phase_status={},
        findings=[],
        metrics={},
        final_status="",
        training_ready=None,
        last_updated=datetime.datetime.now().isoformat(),
    )

    new_findings = _episode_findings(state)
    _record_metrics(state)

    findings_dicts = [
        {
            "episode_path": f.episode_path,
            "phase": f.phase,
            "check_name": f.check_name,
            "severity": f.severity,
            "status": f.status,
            "message": f.message,
            "details": f.details,
        }
        for f in new_findings
    ]
    return episode_path_str, findings_dicts, state.metrics


def _run_phase_parallel(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    workers: int,
) -> list[EpisodeState]:
    pending = [state for state in states if PHASE_NUMBER not in state.phases_completed]
    args = [str(state.episode_path) for state in pending]
    states_by_path = {str(state.episode_path): state for state in pending}

    with Pool(processes=workers) as pool:
        for index, (episode_path_str, findings_dicts, metrics) in enumerate(
            pool.imap_unordered(_process_episode_worker, args)
        ):
            state = states_by_path[episode_path_str]
            new_findings = [Finding(**item) for item in findings_dicts]
            state.metrics.update(metrics)
            _finish_state(state, db_path, new_findings)
            if progress_callback:
                progress_callback(index + 1, len(pending))
    return states


def _episode_findings(state: EpisodeState) -> list[Finding]:
    findings = []
    findings.extend(_check_episode_folder_name(state))
    findings.extend(_check_checksum_manifest(state))
    metadata_findings = _check_metadata_exists_and_valid(state)
    findings.extend(metadata_findings)
    if metadata_findings:
        return findings
    findings.extend(_check_parent_path_structure(state))
    findings.extend(_check_required_metadata_fields(state))
    findings.extend(_check_task_context_consistency(state))
    findings.extend(_check_modalities_match_folders(state))
    findings.extend(_check_required_modality_files(state))
    findings.extend(_check_action_pluralization(state))
    findings.extend(_check_unknown_modalities(state))
    findings.extend(_check_quality_labels(state))
    return findings


def _check_episode_folder_name(state: EpisodeState) -> list[Finding]:
    if state.episode_path.name.startswith("episode_"):
        return []
    return [
        _finding(
            state,
            "episode_folder_name",
            "major",
            "fail",
            "Episode folder name must start with episode_.",
        )
    ]


def _check_metadata_exists_and_valid(state: EpisodeState) -> list[Finding]:
    metadata, findings = load_metadata(state.episode_path)
    if findings:
        return findings
    state.metadata = metadata
    return []


def _check_parent_path_structure(state: EpisodeState) -> list[Finding]:
    parts = state.episode_path.parts
    if len(parts) >= 4 and _is_date_part(parts[-3]) and parts[-2]:
        return []
    return [
        _finding(
            state,
            "parent_path_structure",
            "minor",
            "warning",
            "Parent path should follow <task>/<date>/<operator>/<episode>.",
        )
    ]


def _check_required_metadata_fields(state: EpisodeState) -> list[Finding]:
    findings = []
    metadata = state.metadata
    for field_name in ("task_key", "episode_index", "duration_seconds", "total_frames", "modalities"):
        if _missing_or_invalid_required_field(metadata, field_name):
            findings.append(_missing_field_finding(state, field_name))
    if not _positive_fps(metadata):
        findings.append(_missing_field_finding(state, "fps_actual_or_fps_config"))
    if "quality" not in metadata:
        findings.append(_missing_field_finding(state, "quality"))
    return findings


def _check_task_context_consistency(state: EpisodeState) -> list[Finding]:
    findings = []
    task_key = str(state.metadata.get("task_key", ""))
    task_folder = _task_folder_from_path(state.episode_path, task_key)
    robot = first_present(
        state.metadata.get("robot"),
        _robot_from_episode_name(state.episode_path.name),
        _robot_from_robot_type_folder(state.episode_path),
    )
    robot_tagged_folder = _robot_tagged_task_folder_from_path(state.episode_path)
    if task_key and task_folder and _normalized_name(task_key) != _normalized_name(task_folder):
        findings.append(
            _finding(
                state,
                "task_folder_metadata_mismatch",
                "info",
                "pass",
                "Task folder name does not match metadata task_key.",
                {
                    "task_folder": task_folder,
                    "metadata_task_key": task_key,
                    "fix_items": [
                        {
                            "operation": "move_episode_or_update_metadata_task_key",
                            "current_task_folder": task_folder,
                            "metadata_task_key": task_key,
                        }
                    ],
                    "note": "Detection only. This is a labeling/location issue, not a Phase 1 file-integrity failure.",
                },
            )
        )
    expected = _expected_robot_from_task_names([task_key, robot_tagged_folder, task_folder])
    if expected and not _robot_matches_expected(robot, expected):
        suggested_folder = _suggest_task_folder_for_robot(
            first_present(robot_tagged_folder, task_folder, task_key),
            expected,
            robot,
        )
        findings.append(
            _finding(
                state,
                "task_robot_mismatch",
                "major",
                "fail",
                "Episode robot/source does not match the robot/source indicated by the task folder.",
                {
                    "task_key": task_key,
                    "task_folder": task_folder,
                    "robot_tagged_task_folder": robot_tagged_folder,
                    "expected_robot": expected,
                    "actual_robot": robot,
                    "metadata_robot": str(state.metadata.get("robot", "")),
                    "episode_name_robot": _robot_from_episode_name(state.episode_path.name),
                    "robot_type_folder_robot": _robot_from_robot_type_folder(state.episode_path),
                    "accepted_robot_values": _robot_aliases(expected),
                    "current_episode_path": str(state.episode_path),
                    "suggested_task_folder": suggested_folder,
                    "fix_items": [
                        {
                            "operation": "move_episode_to_matching_robot_task_folder_or_fix_metadata",
                            "current_task_folder": first_present(robot_tagged_folder, task_folder),
                            "suggested_task_folder": suggested_folder,
                            "expected_robot": expected,
                            "actual_robot": robot,
                        }
                    ],
                    "note": "The episode is likely located under the wrong robot-specific task folder, "
                            "or metadata/episode naming is wrong.",
                },
            )
        )
    state.metrics["p1_task_folder"] = task_folder
    state.metrics["p1_task_expected_robot"] = expected or ""
    return findings


def _check_modalities_match_folders(state: EpisodeState) -> list[Finding]:
    findings = []
    modalities = _modalities(state.metadata)
    for modality in modalities:
        if _is_flow_modality(modality):
            continue
        if not _is_known_modality(modality):
            continue
        if _modality_paths(state.episode_path, modality):
            continue
        findings.append(
            _finding(
                state,
                "modality_folder_missing",
                "major",
                "fail",
                "Modality folder is missing.",
                {"modality": modality},
            )
        )
    return findings


def _check_checksum_manifest(state: EpisodeState) -> list[Finding]:
    manifest_path = state.episode_path / CHECKSUM_MANIFEST
    if not manifest_path.exists():
        state.metrics["p1_has_checksum_manifest"] = False
        state.metrics["p1_checksum_manifest_file_count"] = 0
        severity, status = ("critical", "fail") if _checksum_manifest_required() else ("minor", "warning")
        return [
            _finding(
                state,
                "checksum_manifest_missing",
                severity,
                status,
                ".checksum_manifest is missing; episode file completeness cannot be verified.",
                {"file": CHECKSUM_MANIFEST},
            )
        ]
    state.metrics["p1_has_checksum_manifest"] = True
    entries, findings = _load_checksum_manifest(state, manifest_path)
    if findings:
        state.metrics["p1_checksum_manifest_file_count"] = 0
        return findings
    state.metrics["p1_checksum_manifest_file_count"] = len(entries)
    findings.extend(_check_manifest_files_present(state, entries))
    if _verify_checksum_hashes():
        findings.extend(_check_manifest_hashes(state, entries))
    return findings


def _load_checksum_manifest(
    state: EpisodeState, manifest_path: Path
) -> tuple[dict[str, str], list[Finding]]:
    try:
        with manifest_path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except json.JSONDecodeError as exc:
        return {}, [
            _checksum_manifest_invalid(
                state,
                "Manifest is not valid JSON.",
                {"file": CHECKSUM_MANIFEST, "error": str(exc)},
            )
        ]
    except OSError as exc:
        return {}, [
            _checksum_manifest_invalid(
                state,
                "Manifest could not be read.",
                {"file": CHECKSUM_MANIFEST, "error": str(exc)},
            )
        ]
    if not isinstance(data, dict):
        return {}, [
            _checksum_manifest_invalid(
                state,
                "Manifest must contain a JSON object.",
                {"file": CHECKSUM_MANIFEST, "actual_type": type(data).__name__},
            )
        ]
    entries = {}
    invalid_paths = []
    invalid_hashes = []
    for relative_path, digest in data.items():
        if not isinstance(relative_path, str) or not _safe_manifest_path(relative_path):
            invalid_paths.append(relative_path)
            continue
        if not isinstance(digest, str) or _hash_algorithm(digest) is None:
            invalid_hashes.append(relative_path)
            continue
        entries[relative_path] = digest.lower()
    findings = []
    if invalid_paths:
        findings.append(
            _checksum_manifest_invalid(
                state,
                "Manifest contains unsafe or invalid relative paths.",
                {"invalid_paths": _limited_paths(invalid_paths), "invalid_path_count": len(invalid_paths)},
            )
        )
    if invalid_hashes:
        findings.append(
            _checksum_manifest_invalid(
                state,
                "Manifest contains unsupported checksum values.",
                {"invalid_hash_paths": _limited_paths(invalid_hashes), "invalid_hash_count": len(invalid_hashes)},
            )
        )
    if findings:
        return {}, findings
    return entries, []


def _check_manifest_files_present(state: EpisodeState, entries: dict[str, str]) -> list[Finding]:
    missing = []
    not_file = []
    empty = []
    for relative_path in entries:
        file_path = state.episode_path / relative_path
        if not file_path.exists():
            missing.append(relative_path)
        elif not file_path.is_file():
            not_file.append(relative_path)
        else:
            try:
                if file_path.stat().st_size == 0:
                    empty.append(relative_path)
            except OSError:
                missing.append(relative_path)
    state.metrics["p1_checksum_missing_file_count"] = len(missing)
    state.metrics["p1_checksum_empty_file_count"] = len(empty)
    findings = []
    if missing:
        findings.append(
            _finding(
                state,
                "checksum_manifest_file_missing",
                "critical",
                "fail",
                "Files listed in .checksum_manifest are missing.",
                {"missing_paths": _limited_paths(missing), "missing_count": len(missing)},
            )
        )
    if not_file:
        findings.append(
            _finding(
                state,
                "checksum_manifest_path_not_file",
                "critical",
                "fail",
                "Paths listed in .checksum_manifest are not regular files.",
                {"paths": _limited_paths(not_file), "path_count": len(not_file)},
            )
        )
    if empty:
        findings.append(
            _finding(
                state,
                "checksum_manifest_file_empty",
                "major",
                "fail",
                "Files listed in .checksum_manifest are empty.",
                {"empty_paths": _limited_paths(empty), "empty_count": len(empty)},
            )
        )
    return findings


def _check_manifest_hashes(state: EpisodeState, entries: dict[str, str]) -> list[Finding]:
    mismatches = []
    unreadable = []
    for relative_path, expected_digest in entries.items():
        file_path = state.episode_path / relative_path
        if not file_path.is_file():
            continue
        actual_digest = _file_digest(file_path, _hash_algorithm(expected_digest) or "sha256")
        if actual_digest is None:
            unreadable.append(relative_path)
        elif actual_digest != expected_digest:
            mismatches.append(relative_path)
    state.metrics["p1_checksum_hash_mismatch_count"] = len(mismatches)
    findings = []
    if unreadable:
        findings.append(
            _finding(
                state,
                "checksum_hash_unreadable",
                "critical",
                "fail",
                "Files listed in .checksum_manifest could not be read for hash verification.",
                {"paths": _limited_paths(unreadable), "path_count": len(unreadable)},
            )
        )
    if mismatches:
        findings.append(
            _finding(
                state,
                "checksum_hash_mismatch",
                "critical",
                "fail",
                "Files listed in .checksum_manifest do not match their recorded checksums.",
                {"mismatch_paths": _limited_paths(mismatches), "mismatch_count": len(mismatches)},
            )
        )
    return findings


def _checksum_manifest_invalid(state: EpisodeState, message: str, details: dict[str, Any]) -> Finding:
    return _finding(
        state,
        "checksum_manifest_invalid",
        "critical",
        "fail",
        message,
        details,
    )


def _safe_manifest_path(relative_path: str) -> bool:
    path = Path(relative_path)
    return bool(relative_path) and not path.is_absolute() and ".." not in path.parts


def _hash_algorithm(digest: str) -> str | None:
    normalized = digest.lower()
    if not all(char in "0123456789abcdef" for char in normalized):
        return None
    if len(normalized) == 32 and "md5" in _accepted_checksum_algorithms():
        return "md5"
    if len(normalized) == 64 and "sha256" in _accepted_checksum_algorithms():
        return "sha256"
    return None


def _file_digest(path: Path, algorithm: str) -> str | None:
    try:
        hash_obj = hashlib.new(algorithm)
        with path.open("rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                hash_obj.update(chunk)
        return hash_obj.hexdigest()
    except (OSError, ValueError):
        return None


def _verify_checksum_hashes() -> bool:
    return bool(config_value(["phase1_metadata", "checksum", "verify_hashes"], False))


def _checksum_manifest_required() -> bool:
    return bool(config_value(["phase1_metadata", "checksum", "required"], True))


def _accepted_checksum_algorithms() -> set[str]:
    configured = config_value(["phase1_metadata", "checksum", "accepted_algorithms"], ["sha256", "md5"])
    if not isinstance(configured, list):
        return {"sha256", "md5"}
    return {str(item).lower() for item in configured}


def _limited_paths(paths: list[Any]) -> list[str]:
    try:
        limit = int(
            config_value(
                ["phase1_metadata", "checksum", "max_missing_paths_in_details"],
                MAX_DETAIL_PATHS_DEFAULT,
            )
        )
    except (TypeError, ValueError):
        limit = MAX_DETAIL_PATHS_DEFAULT
    return [str(path) for path in paths[:limit]]


def _check_action_pluralization(state: EpisodeState) -> list[Finding]:
    singular_paths = _singular_action_paths(state.episode_path)
    if not singular_paths:
        return []
    status = _singular_action_status()
    severity = _configured_status_severity(status)
    rename_items = [
        {
            "current_name": path.name,
            "suggested_name": _plural_action_name(path.name),
            "operation": "rename_directory_and_update_checksum_manifest",
        }
        for path in singular_paths
    ]
    state.metrics["p1_action_rename_needed_count"] = len(rename_items)
    return [
        _finding(
            state,
            "action_modality_singular_name",
            severity,
            status,
            "Action modality directory uses singular action.* naming; actions.* is the preferred plural form.",
            {
                "paths": [path.name for path in singular_paths],
                "suggested_names": [_plural_action_name(path.name) for path in singular_paths],
                "fix_items": rename_items,
                "note": "Detection only. Do not rename without updating .checksum_manifest.",
            },
        )
    ]


def _check_unknown_modalities(state: EpisodeState) -> list[Finding]:
    unknown = sorted(_unknown_modality_names(state))
    if not unknown:
        return []
    status = _unknown_modality_status()
    severity = _configured_status_severity(status)
    return [
        _finding(
            state,
            "unknown_modality_detected",
            severity,
            status,
            "Unknown modality names were found and were not used for required-file failure decisions.",
            {"modalities": unknown},
        )
    ]


def _singular_action_paths(episode_path: Path) -> list[Path]:
    try:
        return sorted(
            child
            for child in episode_path.iterdir()
            if child.is_dir() and child.name.startswith("action.") and not child.name.startswith("actions.")
        )
    except OSError:
        return []


def _plural_action_name(name: str) -> str:
    return "actions." + name.removeprefix("action.")


def _singular_action_status() -> str:
    status = str(config_value(["phase1_metadata", "modalities", "singular_action_status"], "pass"))
    return status if status in {"pass", "warning", "needs_review", "fail"} else "pass"


def _unknown_modality_status() -> str:
    status = str(config_value(["phase1_metadata", "modalities", "unknown_modality_status"], "pass"))
    return status if status in {"pass", "warning", "needs_review", "fail"} else "pass"


def _configured_status_severity(status: str) -> str:
    if status == "pass":
        return "info"
    if status == "fail":
        return "major"
    return "minor"


def _unknown_modality_names(state: EpisodeState) -> set[str]:
    names = set(_modalities(state.metadata))
    try:
        names.update(
            child.name
            for child in state.episode_path.iterdir()
            if child.is_dir() and _could_be_modality_name(child.name)
        )
    except OSError:
        pass
    return {
        name
        for name in names
        if not _is_known_modality(name) and not _is_flow_modality(name)
    }


def _check_required_modality_files(state: EpisodeState) -> list[Finding]:
    findings = []
    for modality in _modality_names_to_check(state):
        for modality_path in _modality_paths(state.episode_path, modality):
            findings.extend(_check_required_files_for_path(state, modality, modality_path))
    return findings


def _check_quality_labels(state: EpisodeState) -> list[Finding]:
    labels = _quality_labels(state.metadata)
    if labels:
        return []
    return [
        _finding(
            state,
            "quality_labels_missing",
            "minor",
            "warning",
            "quality.labels must be a non-empty list.",
        )
    ]


def _finish_state(state: EpisodeState, db_path: Path, new_findings: list[Finding]) -> None:
    state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings, phase=PHASE_NUMBER, episode_path=str(state.episode_path))


def _record_metrics(state: EpisodeState) -> None:
    modalities = _modalities(state.metadata)
    state.metrics.update(
        {
            "p1_modality_count": len(modalities),
            "p1_image_modality_count": sum(1 for name in modalities if name.startswith("observation.image.")),
            "p1_has_checksum_manifest": (state.episode_path / ".checksum_manifest").exists(),
            "p1_quality_labels": _quality_labels(state.metadata),
        }
    )


def _check_required_files_for_path(
    state: EpisodeState, modality: str, modality_path: Path
) -> list[Finding]:
    findings = []
    for filename in _required_files_for_modality(modality):
        file_path = modality_path / filename
        if not file_path.exists():
            findings.append(_missing_modality_file(state, modality, filename))
        elif file_path.stat().st_size == 0:
            findings.append(_empty_modality_file(state, modality, filename))
    return findings


def _required_files_for_modality(modality: str) -> list[str]:
    if _is_flow_modality(modality):
        return []
    if not _is_known_modality(modality):
        return []
    if _is_image_modality(modality):
        return ["video.mp4", "timestamps.csv"]
    if _is_csv_modality(modality):
        return ["data.csv"]
    return []


def _missing_modality_file(state: EpisodeState, modality: str, filename: str) -> Finding:
    return _finding(
        state,
        "required_modality_file_missing",
        "major",
        "fail",
        "Required modality file is missing.",
        {"modality": modality, "missing_file": filename},
    )


def _empty_modality_file(state: EpisodeState, modality: str, filename: str) -> Finding:
    return _finding(
        state,
        "required_modality_file_empty",
        "major",
        "fail",
        "Required modality file is empty.",
        {"modality": modality, "file": filename},
    )


def _missing_field_finding(state: EpisodeState, field_name: str) -> Finding:
    return _finding(
        state,
        "required_metadata_field",
        "major",
        "fail",
        "Required metadata field is missing or invalid.",
        {"field": field_name},
    )


def _missing_or_invalid_required_field(metadata: dict, field_name: str) -> bool:
    if field_name not in metadata:
        return True
    value = metadata.get(field_name)
    if field_name in {"duration_seconds", "total_frames"}:
        return not is_positive_number(value)
    if field_name == "modalities":
        return not isinstance(value, dict) or not value
    return value is None or value == ""


def _task_folder_from_path(episode_path: Path, task_key: str = "") -> str:
    parts = episode_path.parts
    normalized_task_key = _normalized_name(task_key)
    if normalized_task_key:
        for part in reversed(parts[:-1]):
            if _normalized_name(part) == normalized_task_key:
                return part
    date_index = _date_index_from_parts(parts)
    task_index = _task_index_from_parts(parts, date_index)
    if task_index is not None:
        return parts[task_index]
    return ""


def _robot_tagged_task_folder_from_path(episode_path: Path) -> str:
    """Return the task folder when it carries an appointed robot token."""
    task_folder = _task_folder_from_path(episode_path)
    known_tokens = set(_task_robot_tokens())
    return task_folder if known_tokens & set(_name_tokens(task_folder)) else ""


def _date_index_from_parts(parts: tuple[str, ...]) -> int | None:
    for index, part in enumerate(parts):
        if _is_date_part(part):
            return index
    return None


def _task_index_from_parts(parts: tuple[str, ...], date_index: int | None) -> int | None:
    if date_index is None:
        return None
    if date_index >= 3 and _contains_known_robot_token(parts[date_index - 2]):
        return date_index - 3
    if date_index > 0:
        return date_index - 1
    return None


def _robot_from_robot_type_folder(episode_path: Path) -> str:
    parts = episode_path.parts
    date_index = _date_index_from_parts(parts)
    if date_index is None or date_index < 3:
        return ""
    robot_type_folder = parts[date_index - 2]
    tokens = set(_name_tokens(robot_type_folder))
    for robot in _task_robot_tokens():
        if robot in tokens:
            return robot
    return ""


def _normalized_name(value: str) -> str:
    return "_".join(_name_tokens(value))


def _expected_robot_from_task_names(task_names: list[str]) -> str:
    known_tokens = _task_robot_tokens()
    for task_name in task_names:
        tokens = set(_name_tokens(task_name))
        for expected in known_tokens:
            if expected in tokens:
                return expected
    return ""


def _robot_matches_expected(robot: str, expected: str) -> bool:
    normalized_robot = _normalize_robot_value(robot)
    return bool(normalized_robot) and normalized_robot in _robot_aliases(expected)


def _contains_known_robot_token(value: str) -> bool:
    return bool(set(_task_robot_tokens()) & set(_name_tokens(value)))


def _suggest_task_folder_for_robot(task_folder: str, expected: str, actual_robot: str) -> str:
    actual = _canonical_robot_token(actual_robot)
    if not task_folder or not actual:
        return ""
    expected_aliases = set(_robot_aliases(expected))
    parts = task_folder.split("_")
    replaced = False
    out = []
    for part in parts:
        if _normalize_robot_value(part) in expected_aliases:
            out.append(actual)
            replaced = True
        else:
            out.append(part)
    if replaced:
        return "_".join(out)
    return f"{task_folder}_{actual}"


def _canonical_robot_token(robot: str) -> str:
    normalized = _normalize_robot_value(robot)
    if not normalized:
        return ""
    configured = config_value(["phase1_metadata", "task_robot_tokens"], {})
    if isinstance(configured, dict):
        for key, aliases in configured.items():
            if isinstance(aliases, list) and normalized in {
                _normalize_robot_value(str(item)) for item in aliases
            }:
                return str(key).lower()
    return normalized


def _robot_aliases(expected: str) -> list[str]:
    configured = config_value(["phase1_metadata", "task_robot_tokens"], {})
    if isinstance(configured, dict):
        aliases = configured.get(expected)
        if isinstance(aliases, list):
            return sorted({_normalize_robot_value(str(item)) for item in aliases if str(item)})
    return [expected]


def _task_robot_tokens() -> list[str]:
    configured = config_value(["phase1_metadata", "task_robot_tokens"], {})
    if isinstance(configured, dict):
        return sorted(str(key).lower() for key in configured)
    return []


def _normalize_robot_value(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _robot_from_episode_name(name: str) -> str:
    parts = name.split("_")
    if len(parts) >= 6 and parts[0] == "episode":
        return parts[-2]
    return ""


def _name_tokens(value: str) -> list[str]:
    tokens = []
    current = []
    for char in value.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _positive_fps(metadata: dict) -> Any:
    fps_actual = metadata.get("fps_actual")
    if is_positive_number(fps_actual):
        return fps_actual
    fps_config = metadata.get("fps_config")
    if is_positive_number(fps_config):
        return fps_config
    return first_present(fps_actual, fps_config)


def _modalities(metadata: dict) -> dict:
    modalities = metadata.get("modalities")
    return modalities if isinstance(modalities, dict) else {}


def _quality_labels(metadata: dict) -> list:
    quality = metadata.get("quality")
    if not isinstance(quality, dict):
        return []
    labels = quality.get("labels")
    return labels if isinstance(labels, list) and labels else []


def _modality_paths(episode_path: Path, modality: str) -> list[Path]:
    """Return matching modality directories for a given modality key.

    Handles the case where metadata uses the bare key 'actions' to represent
    all action modalities, while the actual directories are named with suffixes
    such as actions.joint_position or action.eef_pose.
    """
    exact_path = episode_path / modality
    if exact_path.is_dir():
        return [exact_path]
    if modality == "actions":
        return _existing_action_paths(episode_path)
    return []


def _existing_action_paths(episode_path: Path) -> list[Path]:
    """Return all action.* and actions.* subdirectories in the episode folder."""
    try:
        return sorted(
            child
            for child in episode_path.iterdir()
            if child.is_dir()
            and (child.name.startswith("action.") or child.name.startswith("actions."))
        )
    except OSError:
        return []


def _modality_names_to_check(state: EpisodeState) -> list[str]:
    names = set(_modalities(state.metadata))
    try:
        for child in state.episode_path.iterdir():
            if child.is_dir() and _looks_like_modality(child.name):
                names.add(child.name)
    except OSError:
        pass
    return sorted(names)


def _looks_like_modality(name: str) -> bool:
    return _is_known_modality(name) or _is_flow_modality(name)


def _could_be_modality_name(name: str) -> bool:
    return name.startswith("action.") or name.startswith("actions.") or name.startswith("observation.")


def _is_known_modality(modality: str) -> bool:
    return _is_csv_modality(modality) or _is_image_modality(modality)


def _is_csv_modality(modality: str) -> bool:
    return (
        modality == "actions"
        or modality.startswith("action.")
        or modality.startswith("actions.")
        or modality.startswith("observation.state.")
    )


def _is_image_modality(modality: str) -> bool:
    return modality.startswith("observation.image.") and not modality.startswith("observation.image.flow_")


def _is_flow_modality(modality: str) -> bool:
    return modality.startswith("observation.image.flow_")


def _is_date_part(value: str) -> bool:
    return len(value) == 8 and value.isdigit()


def _finding(
    state: EpisodeState,
    check_name: str,
    severity: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> Finding:
    return Finding(
        episode_path=str(state.episode_path),
        phase=PHASE_NUMBER,
        check_name=check_name,
        severity=severity,
        status=status,
        message=message,
        details=details or {},
    )
