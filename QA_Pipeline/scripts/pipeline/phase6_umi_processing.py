"""Phase 6 UMI validation, trajectory preprocessing, and world-frame export."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import importlib
import shutil
import sys
import traceback

from scripts.pipeline.qa_config import config_value
from scripts.pipeline.qa_core import (
    EpisodeState,
    Finding,
    PipelineConfigurationError,
    decide_status,
    load_metadata,
    save_episode_state,
    save_findings,
)


PHASE_NUMBER = 6
DEFAULT_SUFFIX = "_w_world_base"
SCHEMA_VERSION = 1


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run UMI-specific validation and derived-data export for unfinished episodes."""
    pending = [state for state in states if PHASE_NUMBER not in state.phases_completed]
    total = len(pending)
    modules = _load_umi_modules()
    settings = _settings()
    context = _build_context(modules, settings)

    for index, state in enumerate(pending, start=1):
        _ensure_metadata(state)
        findings = _episode_findings(state, context, settings)
        _finish_state(state, db_path, findings)
        if progress_callback:
            progress_callback(index, total)
    return states


def validate_dependencies() -> None:
    """Fail before writing outputs if Phase 6 cannot run in this environment."""
    if not bool(config_value(["phase6_umi_processing", "enabled"], True)):
        return
    missing = []
    for module_name in ("numpy", "scipy", "cv2"):
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            missing.append(binary)
    if missing:
        raise PipelineConfigurationError(
            "Phase 6 UMI processing requires missing dependencies: "
            + ", ".join(sorted(missing))
            + ". Install Python packages into datapipeline-env and install ffmpeg/ffprobe on the host."
        )
    try:
        _load_umi_modules()
    except Exception as exc:  # noqa: BLE001 - convert import/setup errors to pipeline config errors
        raise PipelineConfigurationError(f"Could not load DataProcessUMI modules: {exc}") from exc


def _episode_findings(state: EpisodeState, context: dict[str, Any], settings: dict[str, Any]) -> list[Finding]:
    if not settings["enabled"]:
        state.metrics["p6_umi_status"] = "disabled"
        return [
            _finding(
                state,
                "umi_processing_disabled",
                "info",
                "pass",
                "UMI processing is disabled in configuration.",
            )
        ]
    if not _is_umi_episode(state, settings):
        state.metrics["p6_umi_status"] = "not_applicable"
        return [
            _finding(
                state,
                "umi_not_applicable",
                "info",
                "pass",
                "Episode does not look like UMI data; Phase 6 skipped.",
            )
        ]
    try:
        result = _process_umi_episode(state, context, settings)
    except Exception as exc:  # noqa: BLE001 - one bad episode should not abort the run
        state.metrics["p6_umi_status"] = "error"
        return [
            _finding(
                state,
                "umi_processing_error",
                "critical",
                "fail",
                "UMI processing raised an exception.",
                {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()},
            )
        ]

    status = result["status"]
    state.metrics.update(
        {
            "p6_umi_status": status,
            "p6_umi_failed_stage": result.get("failed_stage") or "",
            "p6_umi_reason": result.get("reason") or "",
            "p6_umi_label": result.get("label") or "",
            "p6_umi_category": result.get("category") or "",
            "p6_umi_operations": result.get("operations") or [],
            "p6_umi_data_path": result.get("data_path") or "",
            "p6_umi_report_path": result.get("report_path") or "",
        }
    )
    if status == "passed":
        state.training_ready = True
        operations = result.get("operations") or []
        if operations and operations != ["passthrough"]:
            return [
                _finding(
                    state,
                    "umi_processed_repaired",
                    "minor",
                    settings["status_for_repaired"],
                    "UMI episode was accepted after preprocessing repair or crop.",
                    result,
                )
            ]
        return [
            _finding(
                state,
                "umi_processed_passed",
                "info",
                "pass",
                "UMI episode passed validation and processing.",
                result,
            )
        ]
    state.training_ready = False
    if status == "rejected":
        return [
            _finding(
                state,
                "umi_processing_rejected",
                "major",
                "fail",
                "UMI episode was rejected by UMI validation or preprocessing.",
                result,
            )
        ]
    return [
        _finding(
            state,
            "umi_processing_error",
            "critical",
            "fail",
            "UMI episode could not be processed.",
            result,
        )
    ]


def _process_umi_episode(state: EpisodeState, context: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    modules = context["modules"]
    vrd = modules["vrd"]
    pp = modules["pp"]
    tf = modules["tf"]

    episode_dir = state.episode_path
    generated_at = vrd.now_utc_iso()
    task_name = _task_name(state)
    episode_name = episode_dir.name
    rel_report_dir = _report_rel_dir(state)
    report_path = context["report_root"] / rel_report_dir / f"{episode_name}.json"
    assess = None
    gate = (False, [], [])
    pre_record = None
    tf_record = None
    status = "error"
    failed_stage = None
    reason = None
    data_path = None

    if context["assess_args"] is not None:
        assess = _assess_episode(vrd, context["assess_args"], episode_dir, task_name, generated_at)
        if assess is not None:
            validation_path = context["work_root"] / "assessment" / rel_report_dir / f"{episode_name}.validation.json"
            vrd.write_json_report(validation_path, assess)
    gate = _assessment_gate(assess)

    if gate[0]:
        status = "rejected"
        failed_stage = "assessment"
        reason = "assessment failed: " + ", ".join(gate[1])
    else:
        cleaned_ep = context["work_root"] / "preprocess" / rel_report_dir / episode_name
        cleaned_ep.parent.mkdir(parents=True, exist_ok=True)
        pre_record = pp.process_episode(
            episode_dir,
            cleaned_ep,
            context["pre_config"],
            context["smooth_config"],
            settings["fps"],
            write=True,
            do_video=True,
            overwrite=True,
        )
        label = pre_record.get("label")
        category = pre_record.get("category")
        if not pre_record.get("ok"):
            status = "rejected" if category == "reject" or pre_record.get("kept_frames") == 0 else "error"
            failed_stage = "preprocess"
            reason = pre_record.get("error") or f"preprocess produced no output ({category})"
        elif category == "reject":
            status = "rejected"
            failed_stage = "preprocess"
            reason = pre_record.get("error") or "unrecoverable trajectory"
        else:
            dest_ep = context["data_root"] / _data_rel_dir(state, settings["suffix"]) / episode_name
            _ensure_safe_output(context["output_root"], dest_ep, settings["overwrite"])
            tf_record = tf.transform_episode(cleaned_ep, dest_ep, context["tf_config"])
            status = "passed"
            data_path = str(dest_ep)

    payload = _combined_report_payload(
        state,
        task_name,
        status,
        reason,
        failed_stage,
        assess,
        gate,
        pre_record,
        tf_record,
        data_path,
        generated_at,
    )
    vrd.write_json_report(report_path, payload)

    if not settings["keep_intermediate"]:
        shutil.rmtree(context["work_root"] / "preprocess" / rel_report_dir / episode_name, ignore_errors=True)

    return {
        "status": status,
        "failed_stage": failed_stage,
        "reason": reason,
        "label": pre_record.get("label") if pre_record else None,
        "category": pre_record.get("category") if pre_record else None,
        "operations": pre_record.get("operations") if pre_record else None,
        "data_path": data_path,
        "report_path": str(report_path),
    }


def _build_context(modules: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    output_root = _resolve_path(settings["output_root"])
    data_root = output_root / "data"
    report_root = output_root / "report"
    work_root = output_root / ".work"

    vrd = modules["vrd"]
    pp = modules["pp"]
    sa = modules["sa"]
    tf = modules["tf"]
    assess_args = None
    if not settings["skip_assessment"]:
        assess_args = vrd.parse_args(settings["assessment_args"].split() if settings["assessment_args"] else [])
        if settings["fps"] is not None:
            assess_args.fps = settings["fps"]
        vrd.apply_validate_config(assess_args)
        vrd.require_check_dependencies(assess_args)

    return {
        "modules": modules,
        "output_root": output_root,
        "data_root": data_root,
        "report_root": report_root,
        "work_root": work_root,
        "assess_args": assess_args,
        "pre_config": pp.load_preprocess_config(settings["preprocess_config"]),
        "smooth_config": sa.load_config(settings["smooth_config"]),
        "tf_config": tf.load_config(settings["transform_config"]),
    }


def _settings() -> dict[str, Any]:
    repaired_status = str(config_value(["phase6_umi_processing", "status_for_repaired"], "warning"))
    if repaired_status not in {"pass", "warning", "needs_review", "fail"}:
        repaired_status = "warning"
    return {
        "enabled": bool(config_value(["phase6_umi_processing", "enabled"], True)),
        "output_root": config_value(["phase6_umi_processing", "output_root"], "outputs/umi_processed"),
        "suffix": str(config_value(["phase6_umi_processing", "suffix"], DEFAULT_SUFFIX) or DEFAULT_SUFFIX),
        "overwrite": bool(config_value(["phase6_umi_processing", "overwrite"], True)),
        "keep_intermediate": bool(config_value(["phase6_umi_processing", "keep_intermediate"], False)),
        "skip_assessment": bool(config_value(["phase6_umi_processing", "skip_assessment"], False)),
        "assessment_args": str(config_value(["phase6_umi_processing", "assessment_args"], "")),
        "fps": config_value(["phase6_umi_processing", "fps"], None),
        "status_for_repaired": repaired_status,
        "umi_tokens": [str(item).lower() for item in config_value(["phase6_umi_processing", "umi_tokens"], ["umi"])],
        "required_modalities": [
            str(item)
            for item in config_value(
                ["phase6_umi_processing", "required_modalities"],
                ["actions.eef_pose", "observation.state.eef_pose", "observation.state.raw_gripper_rotation"],
            )
        ],
        "preprocess_config": _optional_path(config_value(["phase6_umi_processing", "preprocess_config"], None)),
        "smooth_config": _optional_path(config_value(["phase6_umi_processing", "smooth_config"], None)),
        "transform_config": _optional_path(config_value(["phase6_umi_processing", "transform_config"], None)),
    }


def _load_umi_modules() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    umi_root = repo_root / "DataProcessUMI"
    if not umi_root.is_dir():
        raise FileNotFoundError(f"DataProcessUMI directory not found: {umi_root}")
    for rel in ("assessment", "preprocess", "transform"):
        path = umi_root / rel
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return {
        "vrd": importlib.import_module("validate_raw_data"),
        "sa": importlib.import_module("smooth_assessment"),
        "pp": importlib.import_module("preprocess_trajectory"),
        "tf": importlib.import_module("transform_episode_w_world_base"),
    }


def _is_umi_episode(state: EpisodeState, settings: dict[str, Any]) -> bool:
    tokens = set(settings["umi_tokens"])
    for robot_value in (
        state.metadata.get("robot", ""),
        _robot_from_episode_name(state.episode_path.name),
        state.robot,
    ):
        robot_tokens = set(_name_tokens(str(robot_value)))
        if tokens & robot_tokens:
            return True
        if robot_tokens:
            return False

    # Fallback for UMI datasets whose episode folders are plain "episode_0094"
    # and whose robot/source is only encoded in the task or containing folders,
    # e.g. "<task>_umi" or ".../UMI_Headless/UMI/...".
    path_values = (
        state.task,
        state.metadata.get("task_key", ""),
        *state.episode_path.parts[:-1],
    )
    if tokens & set(_name_tokens(" ".join(str(item) for item in path_values))):
        return True
    required_modalities = settings["required_modalities"]
    return bool(required_modalities) and all(
        (state.episode_path / modality / "data.csv").exists()
        for modality in required_modalities
    )


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


def _assess_episode(vrd: Any, assess_args: Any, episode_dir: Path, class_name: str, generated_at: str) -> dict | None:
    try:
        result = vrd.validate_episode(episode_dir, assess_args)
        return vrd.episode_report_payload(class_name, result, assess_args, generated_at)
    except Exception:  # noqa: BLE001 - failed assessment is represented as missing assessment
        return None


def _assessment_gate(report: dict | None) -> tuple[bool, list[str], list[str]]:
    if report is None:
        return False, [], []
    tolerable_video = {
        "video_frame_count_mismatch",
        "missing_timestamps",
        "duplicate_frames_exceed_thresholds",
    }
    info = report.get("info") or {}
    blocking: list[str] = []
    tolerated: list[str] = []

    video = info.get("video") or {}
    if video and not video.get("correct", True):
        for stream in video.get("streams", []):
            for problem in stream.get("problems", []):
                target = tolerated if problem in tolerable_video else blocking
                target.append(f"video:{stream.get('key')}:{problem}")
        for problem in video.get("problems", []) or []:
            target = tolerated if problem in tolerable_video else blocking
            target.append(f"video:{problem}")

    gripper = info.get("gripper") or {}
    if gripper and not gripper.get("correct", True):
        tolerated.append("gripper:incorrect")

    action = info.get("action") or {}
    if action and not action.get("correct", True):
        for problem in action.get("problems") or ["incorrect"]:
            blocking.append(f"action:{problem}")

    return bool(blocking), blocking, tolerated


def _combined_report_payload(
    state: EpisodeState,
    class_name: str,
    status: str,
    reason: str | None,
    failed_stage: str | None,
    assess: dict | None,
    gate: tuple[bool, list[str], list[str]],
    pre_record: dict | None,
    tf_record: dict | None,
    data_path: str | None,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "qa_pipeline_umi_episode_report",
        "generated_at": generated_at,
        "class_name": class_name,
        "episode": state.episode_path.name,
        "source_path": str(state.episode_path),
        "status": status,
        "reason": reason,
        "failed_stage": failed_stage,
        "data_path": data_path,
        "assessment": _assessment_section(assess, gate),
        "classification": {
            "label": pre_record.get("label") if pre_record else None,
            "label_zh": pre_record.get("label_zh") if pre_record else None,
            "category": pre_record.get("category") if pre_record else None,
        },
        "smoothing": _smoothing_section(pre_record) if pre_record else None,
        "transform": tf_record,
    }


def _assessment_section(report: dict | None, gate: tuple[bool, list[str], list[str]]) -> dict | None:
    if report is None:
        return None
    return {
        "correct": report.get("correct"),
        "result": report.get("result"),
        "checks_run": report.get("checks_run"),
        "source_report": report.get("type"),
        "gate": {
            "blocked": gate[0],
            "blocking_problems": gate[1],
            "tolerated_problems": gate[2],
        },
    }


def _smoothing_section(record: dict) -> dict[str, Any]:
    return {
        "operations": record.get("operations"),
        "quality": record.get("quality"),
        "interpolated": record.get("interpolated"),
        "crop": record.get("crop"),
        "original_frames": record.get("original_frames"),
        "kept_frames": record.get("kept_frames"),
    }


def _ensure_safe_output(output_root: Path, destination: Path, overwrite: bool) -> None:
    output_root = output_root.resolve()
    destination = destination.resolve()
    try:
        destination.relative_to(output_root)
    except ValueError as exc:
        raise ValueError(f"Refusing to write UMI output outside output_root: {destination}") from exc
    if destination.exists() and not overwrite:
        raise FileExistsError(f"UMI output already exists: {destination}")


def _data_rel_dir(state: EpisodeState, suffix: str) -> Path:
    task = _task_name(state) + suffix
    parts = [task]
    if state.date:
        parts.append(state.date)
    if state.operator:
        parts.append(state.operator)
    return Path(*parts)


def _report_rel_dir(state: EpisodeState) -> Path:
    parts = [_task_name(state)]
    if state.date:
        parts.append(state.date)
    if state.operator:
        parts.append(state.operator)
    return Path(*parts)


def _task_name(state: EpisodeState) -> str:
    return state.task or str(state.metadata.get("task_key") or state.episode_path.parent.name)


def _resolve_path(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return _resolve_path(value)


def _ensure_metadata(state: EpisodeState) -> None:
    if state.metadata:
        return
    metadata, findings = load_metadata(state.episode_path)
    if not findings:
        state.metadata = metadata


def _finish_state(state: EpisodeState, db_path: Path, new_findings: list[Finding]) -> None:
    if PHASE_NUMBER not in state.phases_completed:
        state.phases_completed.append(PHASE_NUMBER)
    state.phase_status[PHASE_NUMBER] = decide_status(new_findings)
    state.findings.extend(new_findings)
    state.last_updated = datetime.now().isoformat()
    save_episode_state(db_path, state)
    save_findings(db_path, new_findings, phase=PHASE_NUMBER, episode_path=str(state.episode_path))


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
