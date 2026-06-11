"""Phase 1 structure and metadata checks."""

from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable

from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    count_csv_rows,
    decide_status,
    first_present,
    is_positive_number,
    load_metadata,
    save_episode_state,
    save_findings,
)


PHASE_NUMBER = 1


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
        new_findings = _check_episode_folder_name(state)
        metadata_findings = _check_metadata_exists_and_valid(state)
        new_findings.extend(metadata_findings)
        if not metadata_findings:
            new_findings.extend(_check_parent_path_structure(state))
            new_findings.extend(_check_required_metadata_fields(state))
            new_findings.extend(_check_modalities_match_folders(state))
            new_findings.extend(_check_checksum_manifest(state))
            new_findings.extend(_check_required_modality_files(state))
            new_findings.extend(_check_quality_labels(state))
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

    new_findings = _check_episode_folder_name(state)
    metadata_findings = _check_metadata_exists_and_valid(state)
    new_findings.extend(metadata_findings)
    if not metadata_findings:
        new_findings.extend(_check_parent_path_structure(state))
        new_findings.extend(_check_required_metadata_fields(state))
        new_findings.extend(_check_modalities_match_folders(state))
        new_findings.extend(_check_checksum_manifest(state))
        new_findings.extend(_check_required_modality_files(state))
        new_findings.extend(_check_quality_labels(state))
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


def _check_modalities_match_folders(state: EpisodeState) -> list[Finding]:
    findings = []
    modalities = _modalities(state.metadata)
    for modality in modalities:
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
    if (state.episode_path / ".checksum_manifest").exists():
        return []
    return [
        _finding(
            state,
            "checksum_manifest_missing",
            "minor",
            "warning",
            ".checksum_manifest is missing.",
        )
    ]


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
    save_findings(db_path, new_findings)


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
        return ["video.mp4"]
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
    return (
        name.startswith("action.")
        or name.startswith("actions.")
        or name.startswith("observation.state.")
        or name.startswith("observation.image.")
    )


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
