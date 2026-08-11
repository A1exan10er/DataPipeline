"""Phase 6 UMI validation, trajectory preprocessing, world-frame export, and optional IK."""

from __future__ import annotations

from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Callable
import importlib
import hashlib
import json
import os
import stat
import shlex
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
DEFAULT_VIDEO_ONLY_SUFFIX = "_video_aligned"
SCHEMA_VERSION = 1
PROCEDURE_SCHEMA_VERSION = 1
VIDEO_ORIENTATION_PROCEDURE = "umi.video_orientation"
VIDEO_ORIENTATION_SIDECAR_PROCEDURE = "umi.video_orientation_sidecar"
RAW_ASSESSMENT_PROCEDURE = "umi.raw_data_assessment"
TRAJECTORY_GATE_PROCEDURE = "umi.trajectory_gate"
TRAJECTORY_PREPROCESS_PROCEDURE = "umi.trajectory_preprocess"
WORLD_BASE_PROCEDURE = "umi.world_base_pose_transform"
EXECUTABILITY_PROCEDURE = "umi.executability"
PROCEDURE_VERSIONS = {
    VIDEO_ORIENTATION_PROCEDURE: 2,
    VIDEO_ORIENTATION_SIDECAR_PROCEDURE: 2,
    RAW_ASSESSMENT_PROCEDURE: 1,
    TRAJECTORY_GATE_PROCEDURE: 1,
    TRAJECTORY_PREPROCESS_PROCEDURE: 1,
    WORLD_BASE_PROCEDURE: 1,
    EXECUTABILITY_PROCEDURE: 1,
}
_WORKER_CONTEXT: dict[str, Any] | None = None
_WORKER_SETTINGS: dict[str, Any] | None = None


def run_phase(
    states: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None = None,
    workers: int = 1,
) -> list[EpisodeState]:
    """Run UMI-specific validation and derived-data export for unfinished episodes."""
    settings = _settings()
    pending = [
        state
        for state in states
        if PHASE_NUMBER not in state.phases_completed or needs_run(state, settings)
    ]
    total = len(pending)
    _apply_runtime_env(settings)
    modules = _load_umi_modules(load_executability=settings["run_executability"])
    context = _build_context(modules, settings)
    max_parallel = max(1, int(settings["max_concurrent_episodes"]))
    effective_workers = min(max(1, int(workers)), max_parallel, total)

    if effective_workers > 1:
        return _run_phase_parallel(
            states,
            pending,
            db_path,
            progress_callback,
            settings,
            effective_workers,
        )

    for index, state in enumerate(pending, start=1):
        _ensure_metadata(state)
        findings = _episode_findings(state, context, settings)
        _finish_state(state, db_path, findings)
        if progress_callback:
            progress_callback(index, total)
    return states


def _run_phase_parallel(
    states: list[EpisodeState],
    pending: list[EpisodeState],
    db_path: Path,
    progress_callback: Callable[[int, int], None] | None,
    settings: dict[str, Any],
    workers: int,
) -> list[EpisodeState]:
    total = len(pending)
    print(
        f"Phase 6 UMI parallel mode: workers={workers}, "
        f"ik_jobs_per_episode={settings['ik']['jobs']}"
    )
    states_by_path = {str(state.episode_path): state for state in pending}
    payloads = [_state_payload(state) for state in pending]

    max_tasks = settings.get("max_tasks_per_child")
    with Pool(
        processes=workers,
        initializer=_init_phase6_worker,
        initargs=(settings,),
        maxtasksperchild=max_tasks,
    ) as pool:
        for index, result in enumerate(pool.imap_unordered(_process_phase6_worker, payloads), start=1):
            state = states_by_path[result["episode_path"]]
            state.metrics.update(result["metrics"])
            state.training_ready = result["training_ready"]
            findings = [Finding(**item) for item in result["findings"]]
            _finish_state(state, db_path, findings)
            if progress_callback:
                progress_callback(index, total)
    return states


def _init_phase6_worker(settings: dict[str, Any]) -> None:
    global _WORKER_CONTEXT, _WORKER_SETTINGS
    _WORKER_SETTINGS = settings
    _apply_runtime_env(settings)
    modules = _load_umi_modules(load_executability=settings["run_executability"])
    _WORKER_CONTEXT = _build_context(modules, settings)


def _process_phase6_worker(payload: dict[str, Any]) -> dict[str, Any]:
    if _WORKER_CONTEXT is None or _WORKER_SETTINGS is None:
        raise RuntimeError("Phase 6 worker was not initialized")
    state = _state_from_payload(payload)
    _ensure_metadata(state)
    findings = _episode_findings(state, _WORKER_CONTEXT, _WORKER_SETTINGS)
    return {
        "episode_path": str(state.episode_path),
        "metrics": state.metrics,
        "training_ready": state.training_ready,
        "findings": [_finding_payload(item) for item in findings],
    }


def _state_payload(state: EpisodeState) -> dict[str, Any]:
    return {
        "episode_path": str(state.episode_path),
        "task": state.task,
        "date": state.date,
        "operator": state.operator,
        "robot": state.robot,
        "controller": state.controller,
        "metadata": state.metadata,
        "metrics": state.metrics,
        "training_ready": state.training_ready,
    }


def _state_from_payload(payload: dict[str, Any]) -> EpisodeState:
    return EpisodeState(
        episode_path=Path(payload["episode_path"]),
        task=str(payload.get("task") or ""),
        date=str(payload.get("date") or ""),
        operator=str(payload.get("operator") or ""),
        robot=str(payload.get("robot") or ""),
        controller=str(payload.get("controller") or ""),
        metadata=dict(payload.get("metadata") or {}),
        metrics=dict(payload.get("metrics") or {}),
        training_ready=payload.get("training_ready"),
    )


def _finding_payload(finding: Finding) -> dict[str, Any]:
    return {
        "episode_path": finding.episode_path,
        "phase": finding.phase,
        "check_name": finding.check_name,
        "severity": finding.severity,
        "status": finding.status,
        "message": finding.message,
        "details": finding.details,
    }


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
    if (
        str(
            os.environ.get("QA_PHASE6_PROFILE")
            or config_value(["phase6_umi_processing", "profile"], "full")
        )
        == "full"
        and bool(config_value(["phase6_umi_processing", "run_executability"], False))
    ):
        try:
            importlib.import_module("pinocchio")
            modules = _load_umi_modules(load_executability=True)
            se = modules["se"]
            robots = modules["robots"]
            selected = config_value(["phase6_umi_processing", "ik", "robots"], None)
            robot_names = selected or robots.list_robots()
            for robot_name in robot_names:
                if robot_name not in robots.list_robots():
                    raise KeyError(f"unknown IK robot '{robot_name}'")
            # Importing solve_executability validates most internal module wiring.
            getattr(se, "main")
        except Exception as exc:  # noqa: BLE001
            raise PipelineConfigurationError(
                "Phase 6 UMI executability is enabled but IK dependencies/resources "
                f"are unavailable: {type(exc).__name__}: {exc}"
            ) from exc


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
            "p6_umi_trajectory_gate": result.get("trajectory_gate") or {},
            "p6_umi_executability": result.get("executability") or {},
            "p6_umi_profile": settings["profile"],
            "p6_umi_requested_procedures": result.get("requested_procedures") or [],
            "p6_umi_executed_procedures": result.get("executed_procedures") or [],
            "p6_umi_skipped_procedures": result.get("skipped_procedures") or [],
        }
    )
    if status == "passed":
        if settings["profile"] == "full":
            state.training_ready = True
        if settings["profile"] == "video_only":
            return [
                _finding(
                    state,
                    "umi_video_orientation_passed",
                    "info",
                    "pass",
                    "UMI wrist videos were normalized to the configured orientation.",
                    result,
                )
            ]
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
        if result.get("failed_stage") == "trajectory":
            return [
                _finding(
                    state,
                    "umi_trajectory_gate_rejected",
                    "major",
                    settings["trajectory_nonpass_status"],
                    "UMI episode did not pass the strict trajectory-first gate.",
                    result,
                )
            ]
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


def _process_video_only_episode(
    state: EpisodeState,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    vrd = context["modules"]["vrd"]
    tf = context["modules"]["tf"]
    source_episode = state.episode_path.resolve()
    sidecar_mode = settings["video"]["output_mode"] == "sidecar"
    destination = (
        source_episode
        if sidecar_mode
        else _profile_destination(state, context, settings, "video_only")
    )
    if not sidecar_mode:
        # Check containment first; existing valid outputs are handled as
        # resumable procedure state rather than as a generic overwrite.
        _ensure_safe_output(context["output_root"], destination, overwrite=True)
    rotations = settings["video"]["orientation_correction"]
    procedure = (
        VIDEO_ORIENTATION_SIDECAR_PROCEDURE
        if sidecar_mode
        else VIDEO_ORIENTATION_PROCEDURE
    )
    procedure_config = _procedure_config(procedure, settings, context)
    expected_outputs = _procedure_outputs(procedure, settings)
    requested = [procedure]

    if _procedure_complete(
        destination,
        procedure,
        procedure_config,
        expected_outputs,
    ):
        executed: list[str] = []
        skipped = requested.copy()
    else:
        if not sidecar_mode and destination.exists() and not settings["overwrite"]:
            raise FileExistsError(
                "UMI video-only output exists but its procedure receipt is missing or stale; "
                f"enable overwrite to rebuild it: {destination}"
            )
        if sidecar_mode:
            rotated = tf.rotate_wrist_videos_to_sidecars(
                source_episode,
                rotations,
                settings["video"]["sidecar_filename"],
            )
            _record_procedure(
                source_episode,
                procedure,
                procedure_config,
                rotated,
                source_episode,
            )
            tf.recompute_checksum_manifest(source_episode)
        else:
            staging = destination.with_name(f".{destination.name}.video-orientation.tmp-{os.getpid()}")
            if staging.exists():
                shutil.rmtree(staging)
            staging.parent.mkdir(parents=True, exist_ok=True)
            resume_source = (
                destination
                if destination.exists() and _recorded_source(destination) is not None
                else source_episode
            )
            _validate_resume_source(resume_source, source_episode)
            shutil.copytree(resume_source, staging)
            try:
                rotated = tf.rotate_wrist_videos(staging, rotations)
                _record_procedure(
                    staging,
                    procedure,
                    procedure_config,
                    rotated,
                    source_episode,
                )
                tf.recompute_checksum_manifest(staging)
                _publish_directory(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        executed = requested.copy()
        skipped = []

    generated_at = vrd.now_utc_iso()
    report_path = (
        context["report_root"]
        / _report_rel_dir(state)
        / f"{state.episode_path.name}.json"
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "type": "qa_pipeline_umi_episode_report",
        "generated_at": generated_at,
        "class_name": _task_name(state),
        "episode": state.episode_path.name,
        "source_path": str(source_episode),
        "status": "passed",
        "profile": "video_only",
        "video_output_mode": settings["video"]["output_mode"],
        "data_path": str(destination),
        "requested_procedures": requested,
        "executed_procedures": executed,
        "skipped_procedures": skipped,
    }
    vrd.write_json_report(report_path, payload)
    return {
        "status": "passed",
        "failed_stage": None,
        "reason": None,
        "label": None,
        "category": None,
        "operations": requested,
        "requested_procedures": requested,
        "executed_procedures": executed,
        "skipped_procedures": skipped,
        "data_path": str(destination),
        "report_path": str(report_path),
        "trajectory_gate": None,
        "executability": None,
    }


def _process_umi_episode(state: EpisodeState, context: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    if settings["profile"] == "video_only":
        return _process_video_only_episode(state, context, settings)

    modules = context["modules"]
    vrd = modules["vrd"]
    pp = modules["pp"]
    tf = modules["tf"]
    sa = modules["sa"]

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
    exec_record = None
    trajectory_gate = None
    status = "error"
    failed_stage = None
    reason = None
    data_path = None
    requested_procedures = _requested_procedures(settings)
    executed_procedures: list[str] = []
    skipped_procedures: list[str] = []
    destination = _profile_destination(state, context, settings, "full")

    if all(
        _procedure_complete(
            destination,
            procedure,
            _procedure_config(procedure, settings, context),
            _procedure_outputs(procedure, settings),
        )
        for procedure in requested_procedures
    ):
        data_path = str(destination)
        status = "passed"
        skipped_procedures = requested_procedures.copy()
        payload = _combined_report_payload(
            state,
            task_name,
            status,
            None,
            None,
            None,
            (False, [], []),
            None,
            None,
            None,
            None,
            data_path,
            generated_at,
        )
        payload.update(
            {
                "profile": "full",
                "requested_procedures": requested_procedures,
                "executed_procedures": [],
                "skipped_procedures": skipped_procedures,
            }
        )
        vrd.write_json_report(report_path, payload)
        return {
            "status": status,
            "failed_stage": None,
            "reason": None,
            "label": None,
            "category": None,
            "operations": [],
            "requested_procedures": requested_procedures,
            "executed_procedures": [],
            "skipped_procedures": skipped_procedures,
            "data_path": data_path,
            "report_path": str(report_path),
            "trajectory_gate": None,
            "executability": None,
        }

    if settings["trajectory_first_gate"]:
        trajectory_gate = _trajectory_gate(sa, episode_dir, context["smooth_config"], settings)
        if trajectory_gate["status"] != "pass":
            status = "rejected"
            failed_stage = "trajectory"
            reason = trajectory_gate["reason"]

    if status != "rejected" and context["assess_args"] is not None:
        assess = _assess_episode(vrd, context["assess_args"], episode_dir, task_name, generated_at)
        if assess is not None:
            validation_path = context["work_root"] / "assessment" / rel_report_dir / f"{episode_name}.validation.json"
            vrd.write_json_report(validation_path, assess)
    gate = _assessment_gate(assess)

    if status == "rejected":
        pass
    elif gate[0]:
        status = "rejected"
        failed_stage = "assessment"
        reason = "assessment failed: " + ", ".join(gate[1])
    else:
        preprocess_source = _full_resume_source(state, context, settings)
        preprocess_source, sidecar_materialized = _materialize_sidecar_input(
            state,
            preprocess_source,
            context,
            settings,
        )
        cleaned_ep = context["work_root"] / "preprocess" / rel_report_dir / episode_name
        cleaned_ep.parent.mkdir(parents=True, exist_ok=True)
        pre_record = pp.process_episode(
            preprocess_source,
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
            dest_ep = destination
            _ensure_safe_output(context["output_root"], dest_ep, settings["overwrite"])
            video_config = _procedure_config(VIDEO_ORIENTATION_PROCEDURE, settings, context)
            video_outputs = _procedure_outputs(VIDEO_ORIENTATION_PROCEDURE, settings)
            video_already_done = sidecar_materialized or _procedure_complete(
                cleaned_ep,
                VIDEO_ORIENTATION_PROCEDURE,
                video_config,
                video_outputs,
            )
            tf_record = tf.transform_episode(
                cleaned_ep,
                dest_ep,
                context["tf_config"],
                rotate_videos=False,
            )
            _record_procedure(
                dest_ep,
                TRAJECTORY_PREPROCESS_PROCEDURE,
                _procedure_config(TRAJECTORY_PREPROCESS_PROCEDURE, settings, context),
                _procedure_outputs(TRAJECTORY_PREPROCESS_PROCEDURE, settings),
                episode_dir,
            )
            if video_already_done:
                skipped_procedures.append(VIDEO_ORIENTATION_PROCEDURE)
                if sidecar_materialized:
                    _record_procedure(
                        dest_ep,
                        VIDEO_ORIENTATION_PROCEDURE,
                        video_config,
                        video_outputs,
                        episode_dir,
                    )
            else:
                rotated_videos = tf.rotate_wrist_videos(
                    dest_ep,
                    settings["video"]["orientation_correction"],
                )
                tf_record["rotated_videos"] = rotated_videos
                _record_procedure(
                    dest_ep,
                    VIDEO_ORIENTATION_PROCEDURE,
                    video_config,
                    rotated_videos,
                    episode_dir,
                )
                executed_procedures.append(VIDEO_ORIENTATION_PROCEDURE)
            _record_procedure(
                dest_ep,
                WORLD_BASE_PROCEDURE,
                _procedure_config(WORLD_BASE_PROCEDURE, settings, context),
                tf_record.get("transformed_files") or [],
                episode_dir,
            )
            executed_procedures.extend(
                [TRAJECTORY_PREPROCESS_PROCEDURE, WORLD_BASE_PROCEDURE]
            )
            if settings["trajectory_first_gate"]:
                _record_procedure(
                    dest_ep,
                    TRAJECTORY_GATE_PROCEDURE,
                    _procedure_config(TRAJECTORY_GATE_PROCEDURE, settings, context),
                    [],
                    episode_dir,
                )
                executed_procedures.append(TRAJECTORY_GATE_PROCEDURE)
            if assess is not None:
                _record_procedure(
                    dest_ep,
                    RAW_ASSESSMENT_PROCEDURE,
                    _procedure_config(RAW_ASSESSMENT_PROCEDURE, settings, context),
                    [],
                    episode_dir,
                )
                executed_procedures.append(RAW_ASSESSMENT_PROCEDURE)
            if settings["run_executability"]:
                exec_out = context["report_root"] / rel_report_dir / episode_name / "executability"
                exec_record = _run_executability(modules, settings, dest_ep, exec_out)
                _record_procedure(
                    dest_ep,
                    EXECUTABILITY_PROCEDURE,
                    _procedure_config(EXECUTABILITY_PROCEDURE, settings, context),
                    [],
                    episode_dir,
                )
                executed_procedures.append(EXECUTABILITY_PROCEDURE)
            tf.recompute_checksum_manifest(dest_ep)
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
        exec_record,
        trajectory_gate,
        data_path,
        generated_at,
    )
    payload.update(
        {
            "profile": "full",
            "requested_procedures": requested_procedures,
            "executed_procedures": executed_procedures,
            "skipped_procedures": skipped_procedures,
        }
    )
    vrd.write_json_report(report_path, payload)

    if not settings["keep_intermediate"]:
        shutil.rmtree(context["work_root"] / "preprocess" / rel_report_dir / episode_name, ignore_errors=True)
        shutil.rmtree(
            context["work_root"] / "orientation_input" / rel_report_dir / episode_name,
            ignore_errors=True,
        )

    return {
        "status": status,
        "failed_stage": failed_stage,
        "reason": reason,
        "label": pre_record.get("label") if pre_record else None,
        "category": pre_record.get("category") if pre_record else None,
        "operations": pre_record.get("operations") if pre_record else None,
        "requested_procedures": requested_procedures,
        "executed_procedures": executed_procedures,
        "skipped_procedures": skipped_procedures,
        "data_path": data_path,
        "report_path": str(report_path),
        "trajectory_gate": trajectory_gate,
        "executability": _executability_section(exec_record),
    }


def _build_context(modules: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    output_root = _resolve_path(settings["output_root"])
    data_root = output_root / "data"
    report_root = output_root / "report"
    work_root = output_root / ".work"

    vrd = modules["vrd"]
    if settings["profile"] == "video_only":
        return {
            "modules": modules,
            "output_root": output_root,
            "data_root": data_root,
            "report_root": report_root,
            "work_root": work_root,
        }

    pp = modules["pp"]
    sa = modules["sa"]
    tf = modules["tf"]
    assess_args = None
    if settings["profile"] == "full" and not settings["skip_assessment"]:
        assess_args = vrd.parse_args(settings["assessment_args"].split() if settings["assessment_args"] else [])
        if settings["fps"] is not None:
            assess_args.fps = settings["fps"]
        vrd.apply_validate_config(assess_args)
        vrd.require_check_dependencies(assess_args)

    pre_config = pp.load_preprocess_config(settings["preprocess_config"])
    if settings["video"]["preprocess_preset"]:
        pre_config["video_preset"] = settings["video"]["preprocess_preset"]
    if settings["video"]["ffmpeg_threads"] is not None:
        pre_config["video_threads"] = settings["video"]["ffmpeg_threads"]

    return {
        "modules": modules,
        "output_root": output_root,
        "data_root": data_root,
        "report_root": report_root,
        "work_root": work_root,
        "assess_args": assess_args,
        "pre_config": pre_config,
        "smooth_config": sa.load_config(settings["smooth_config"]),
        "tf_config": tf.load_config(settings["transform_config"]),
    }


def _settings() -> dict[str, Any]:
    profile = str(
        os.environ.get("QA_PHASE6_PROFILE")
        or config_value(["phase6_umi_processing", "profile"], "full")
    )
    if profile not in {"full", "video_only"}:
        raise PipelineConfigurationError(
            f"Unsupported phase6_umi_processing.profile: {profile!r}; expected 'full' or 'video_only'."
        )
    repaired_status = str(config_value(["phase6_umi_processing", "status_for_repaired"], "warning"))
    if repaired_status not in {"pass", "warning", "needs_review", "fail"}:
        repaired_status = "warning"
    trajectory_nonpass_status = str(
        config_value(["phase6_umi_processing", "trajectory_nonpass_status"], "fail")
    )
    if trajectory_nonpass_status not in {"fail", "needs_review"}:
        trajectory_nonpass_status = "fail"
    ik_config = config_value(["phase6_umi_processing", "ik"], {}) or {}
    video_config = config_value(["phase6_umi_processing", "video"], {}) or {}
    ffmpeg_threads = video_config.get("ffmpeg_threads", 2)
    if ffmpeg_threads is not None:
        ffmpeg_threads = max(1, int(ffmpeg_threads))
    rotations = video_config.get(
        "orientation_correction",
        {
            "observation.image.left_wrist_view": 180,
            "observation.image.right_wrist_view": 180,
        },
    )
    if not isinstance(rotations, dict) or not rotations:
        raise PipelineConfigurationError("Phase 6 video orientation_correction must be a non-empty object.")
    rotations = {str(name): int(degrees) for name, degrees in rotations.items()}
    unsupported = {name: degrees for name, degrees in rotations.items() if degrees not in (0, 180)}
    if unsupported:
        raise PipelineConfigurationError(
            f"Phase 6 currently supports only 0/180-degree video rotations: {unsupported}"
        )
    video_output_mode = str(
        os.environ.get("QA_PHASE6_VIDEO_OUTPUT_MODE")
        or video_config.get("output_mode", "derived_copy")
    )
    if video_output_mode not in {"derived_copy", "sidecar"}:
        raise PipelineConfigurationError(
            "Phase 6 video output_mode must be 'derived_copy' or 'sidecar'."
        )
    sidecar_filename = str(
        os.environ.get("QA_PHASE6_SIDECAR_FILENAME")
        or video_config.get("sidecar_filename", "video_rotated_180.mp4")
    )
    if not sidecar_filename or Path(sidecar_filename).name != sidecar_filename:
        raise PipelineConfigurationError("Phase 6 video sidecar_filename must be a plain filename.")
    return {
        "profile": profile,
        "enabled": bool(config_value(["phase6_umi_processing", "enabled"], True)),
        "output_root": config_value(["phase6_umi_processing", "output_root"], "outputs/umi_processed"),
        "suffix": str(config_value(["phase6_umi_processing", "suffix"], DEFAULT_SUFFIX) or DEFAULT_SUFFIX),
        "video_only_suffix": str(
            config_value(
                ["phase6_umi_processing", "video_only_suffix"],
                DEFAULT_VIDEO_ONLY_SUFFIX,
            )
            or DEFAULT_VIDEO_ONLY_SUFFIX
        ),
        "overwrite": bool(config_value(["phase6_umi_processing", "overwrite"], True)),
        "keep_intermediate": bool(config_value(["phase6_umi_processing", "keep_intermediate"], False)),
        "skip_assessment": bool(config_value(["phase6_umi_processing", "skip_assessment"], False)),
        "assessment_args": str(config_value(["phase6_umi_processing", "assessment_args"], "")),
        "fps": config_value(["phase6_umi_processing", "fps"], None),
        "status_for_repaired": repaired_status,
        "trajectory_first_gate": bool(config_value(["phase6_umi_processing", "trajectory_first_gate"], True)),
        "trajectory_pass_labels": [
            str(item)
            for item in config_value(["phase6_umi_processing", "trajectory_pass_labels"], ["smooth"])
        ],
        "trajectory_nonpass_status": trajectory_nonpass_status,
        "run_executability": profile == "full"
        and bool(config_value(["phase6_umi_processing", "run_executability"], False)),
        "max_concurrent_episodes": int(
            config_value(["phase6_umi_processing", "max_concurrent_episodes"], 1)
        ),
        "max_tasks_per_child": _positive_int_or_none(
            config_value(["phase6_umi_processing", "max_tasks_per_child"], 10)
        ),
        "ik": {
            "robots": ik_config.get("robots"),
            "arm": str(ik_config.get("arm", "both")),
            "source": str(ik_config.get("source", "action")),
            "max_points": int(ik_config.get("max_points", 200)),
            "min_segment": int(ik_config.get("min_segment", 5)),
            "jobs": int(ik_config.get("jobs", 1)),
            "samples": int(ik_config.get("samples", 80000)),
            "extra_args": str(ik_config.get("extra_args", "")),
        },
        "video": {
            "ffmpeg_threads": ffmpeg_threads,
            "orientation_correction": rotations,
            "output_mode": video_output_mode,
            "sidecar_filename": sidecar_filename,
            "preprocess_preset": (
                None
                if video_config.get("preprocess_preset") in (None, "")
                else str(video_config.get("preprocess_preset"))
            ),
        },
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


def _requested_procedures(settings: dict[str, Any]) -> list[str]:
    if settings["profile"] == "video_only":
        if settings["video"]["output_mode"] == "sidecar":
            return [VIDEO_ORIENTATION_SIDECAR_PROCEDURE]
        return [VIDEO_ORIENTATION_PROCEDURE]
    procedures = []
    if not settings["skip_assessment"]:
        procedures.append(RAW_ASSESSMENT_PROCEDURE)
    if settings["trajectory_first_gate"]:
        procedures.append(TRAJECTORY_GATE_PROCEDURE)
    procedures.extend(
        [
            TRAJECTORY_PREPROCESS_PROCEDURE,
            VIDEO_ORIENTATION_PROCEDURE,
            WORLD_BASE_PROCEDURE,
        ]
    )
    if settings["run_executability"]:
        procedures.append(EXECUTABILITY_PROCEDURE)
    return procedures


def _procedure_config(
    procedure: str,
    settings: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if procedure == VIDEO_ORIENTATION_PROCEDURE:
        return {
            "rotations_degrees": settings["video"]["orientation_correction"],
            "output": "canonical_video",
            "encoding": "preserve_source_stream_format_v1",
            "encoder_preset": settings["video"].get("preprocess_preset", "veryfast"),
            "encoder_threads": settings["video"].get("ffmpeg_threads", 2),
        }
    if procedure == VIDEO_ORIENTATION_SIDECAR_PROCEDURE:
        return {
            "rotations_degrees": settings["video"]["orientation_correction"],
            "output": "sidecar",
            "sidecar_filename": settings["video"]["sidecar_filename"],
            "encoding": "preserve_source_stream_format_v1",
            "encoder_preset": settings["video"].get("preprocess_preset", "veryfast"),
            "encoder_threads": settings["video"].get("ffmpeg_threads", 2),
        }
    if procedure == RAW_ASSESSMENT_PROCEDURE:
        return {"assessment_args": settings["assessment_args"], "fps": settings["fps"]}
    if procedure == TRAJECTORY_GATE_PROCEDURE:
        return {
            "smooth_config": context["smooth_config"],
            "pass_labels": settings["trajectory_pass_labels"],
            "fps": settings["fps"],
        }
    if procedure == TRAJECTORY_PREPROCESS_PROCEDURE:
        return {
            "preprocess_config": context["pre_config"],
            "smooth_config": context["smooth_config"],
            "fps": settings["fps"],
        }
    if procedure == WORLD_BASE_PROCEDURE:
        return {"transform_config": context["tf_config"]}
    if procedure == EXECUTABILITY_PROCEDURE:
        return {"ik": settings["ik"]}
    raise KeyError(f"Unknown UMI procedure: {procedure}")


def _procedure_outputs(procedure: str, settings: dict[str, Any]) -> list[str]:
    if procedure == VIDEO_ORIENTATION_PROCEDURE:
        return _video_output_paths(settings["video"]["orientation_correction"])
    if procedure == VIDEO_ORIENTATION_SIDECAR_PROCEDURE:
        return _video_output_paths(
            settings["video"]["orientation_correction"],
            settings["video"]["sidecar_filename"],
        )
    if procedure in {TRAJECTORY_PREPROCESS_PROCEDURE, WORLD_BASE_PROCEDURE}:
        return [
            "actions.eef_pose/data.csv",
            "observation.state.eef_pose/data.csv",
        ]
    return []


def _video_output_paths(
    rotations: dict[str, int],
    filename: str = "video.mp4",
) -> list[str]:
    return [f"{stream}/{filename}" for stream, degrees in rotations.items() if int(degrees) != 0]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _episode_metadata(episode_dir: Path) -> dict[str, Any]:
    metadata_path = Path(episode_dir) / "metadata.json"
    if not metadata_path.is_file():
        return {}
    with metadata_path.open("r", encoding="utf-8") as file_obj:
        metadata = json.load(file_obj)
    if not isinstance(metadata, dict):
        raise ValueError(f"metadata.json must contain an object: {metadata_path}")
    return metadata


def _procedure_receipts(metadata: dict[str, Any]) -> dict[str, Any]:
    pipeline = metadata.get("data_pipeline")
    if not isinstance(pipeline, dict):
        return {}
    receipts = pipeline.get("completed_procedures")
    return receipts if isinstance(receipts, dict) else {}


def _procedure_complete(
    episode_dir: Path,
    procedure: str,
    config: dict[str, Any],
    expected_outputs: list[str],
) -> bool:
    episode_dir = Path(episode_dir)
    if not episode_dir.is_dir():
        return False
    try:
        receipt = _procedure_receipts(_episode_metadata(episode_dir)).get(procedure)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    if receipt.get("version") != PROCEDURE_VERSIONS[procedure]:
        return False
    if receipt.get("config_hash") != _canonical_hash(config):
        return False
    for relative_path in expected_outputs:
        output_path = episode_dir / relative_path
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            return False
    return True


def _record_procedure(
    episode_dir: Path,
    procedure: str,
    config: dict[str, Any],
    outputs: list[str],
    source_episode: Path,
) -> None:
    episode_dir = Path(episode_dir)
    metadata_path = episode_dir / "metadata.json"
    metadata = _episode_metadata(episode_dir)
    pipeline = metadata.setdefault("data_pipeline", {})
    if not isinstance(pipeline, dict):
        raise ValueError(f"metadata.json data_pipeline must be an object: {metadata_path}")
    pipeline["schema_version"] = PROCEDURE_SCHEMA_VERSION
    pipeline.setdefault("source_episode", str(Path(source_episode).resolve()))
    receipts = pipeline.setdefault("completed_procedures", {})
    if not isinstance(receipts, dict):
        raise ValueError(f"metadata.json completed_procedures must be an object: {metadata_path}")
    receipts[procedure] = {
        "version": PROCEDURE_VERSIONS[procedure],
        "completed_at": datetime.now().astimezone().isoformat(),
        "config": config,
        "config_hash": _canonical_hash(config),
        "outputs": sorted(str(path) for path in outputs),
    }
    temp_path = metadata_path.with_name(f".{metadata_path.name}.tmp-{os.getpid()}")
    episode_mode = stat.S_IMODE(episode_dir.stat().st_mode)
    metadata_mode = (
        stat.S_IMODE(metadata_path.stat().st_mode)
        if metadata_path.exists()
        else None
    )
    try:
        if not episode_mode & stat.S_IWUSR:
            episode_dir.chmod(episode_mode | stat.S_IWUSR)
        for orphan in episode_dir.glob(f".{metadata_path.name}.tmp-*"):
            orphan.unlink()
        with temp_path.open("w", encoding="utf-8") as file_obj:
            json.dump(metadata, file_obj, indent=2, ensure_ascii=False, default=str)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        temp_path.replace(metadata_path)
        if metadata_mode is not None:
            metadata_path.chmod(metadata_mode)
        try:
            descriptor = os.open(str(episode_dir), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Some network filesystems do not support directory fsync.
            pass
    finally:
        if temp_path.exists():
            temp_path.unlink()
        episode_dir.chmod(episode_mode)


def _profile_destination(
    state: EpisodeState,
    context: dict[str, Any],
    settings: dict[str, Any],
    profile: str,
) -> Path:
    suffix = settings["video_only_suffix"] if profile == "video_only" else settings["suffix"]
    return context["data_root"] / _data_rel_dir(state, suffix) / state.episode_path.name


def _validate_resume_source(candidate: Path, source_episode: Path) -> None:
    recorded = _recorded_source(candidate)
    if recorded and Path(str(recorded)).resolve() != Path(source_episode).resolve():
        raise ValueError(
            f"Processed UMI episode belongs to another source: {recorded}; expected {source_episode}"
        )


def _recorded_source(episode_dir: Path) -> str | None:
    pipeline = _episode_metadata(episode_dir).get("data_pipeline")
    if not isinstance(pipeline, dict):
        return None
    value = pipeline.get("source_episode")
    return str(value) if value else None


def _publish_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}.backup-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        destination.replace(backup)
    try:
        staging.replace(destination)
    except Exception:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _full_resume_source(
    state: EpisodeState,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> Path:
    source = state.episode_path.resolve()
    full_destination = _profile_destination(state, context, settings, "full")
    if full_destination.is_dir() and _recorded_source(full_destination) is not None:
        _validate_resume_source(full_destination, source)
        return full_destination
    video_destination = _profile_destination(state, context, settings, "video_only")
    video_config = _procedure_config(VIDEO_ORIENTATION_PROCEDURE, settings, context)
    if _procedure_complete(
        video_destination,
        VIDEO_ORIENTATION_PROCEDURE,
        video_config,
        _video_output_paths(settings["video"]["orientation_correction"]),
    ):
        _validate_resume_source(video_destination, source)
        return video_destination
    return source


def _materialize_sidecar_input(
    state: EpisodeState,
    preprocess_source: Path,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[Path, bool]:
    """Build a work copy whose canonical videos come from rotated sidecars."""
    sidecar_config = _procedure_config(
        VIDEO_ORIENTATION_SIDECAR_PROCEDURE,
        settings,
        context,
    )
    sidecar_outputs = _procedure_outputs(VIDEO_ORIENTATION_SIDECAR_PROCEDURE, settings)
    candidate = None
    for path in (Path(preprocess_source), state.episode_path.resolve()):
        if _procedure_complete(
            path,
            VIDEO_ORIENTATION_SIDECAR_PROCEDURE,
            sidecar_config,
            sidecar_outputs,
        ):
            candidate = path
            break
    if candidate is None:
        return Path(preprocess_source), False

    work_episode = (
        context["work_root"]
        / "orientation_input"
        / _report_rel_dir(state)
        / state.episode_path.name
    )
    if work_episode.exists():
        shutil.rmtree(work_episode)
    work_episode.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(candidate, work_episode)
    sidecar_filename = settings["video"]["sidecar_filename"]
    for stream, degrees in settings["video"]["orientation_correction"].items():
        if int(degrees) == 0:
            continue
        sidecar = work_episode / stream / sidecar_filename
        canonical = work_episode / stream / "video.mp4"
        shutil.copy2(sidecar, canonical)
    return work_episode, True


def needs_run(state: EpisodeState, settings: dict[str, Any] | None = None) -> bool:
    """Return whether the configured Phase 6 profile has uncompleted procedures."""
    settings = settings or _settings()
    if not settings["enabled"] or not _is_umi_episode(state, settings):
        return PHASE_NUMBER not in state.phases_completed
    requested = set(_requested_procedures(settings))
    previous = set(state.metrics.get("p6_umi_requested_procedures") or [])
    if not requested.issubset(previous):
        return True
    data_path = state.metrics.get("p6_umi_data_path")
    if not data_path:
        return True
    receipts = _procedure_receipts(_episode_metadata(Path(str(data_path))))
    return not requested.issubset(receipts)


def _positive_int_or_none(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_umi_state(state: EpisodeState) -> bool:
    """Return whether a state should be handled by Phase 6 UMI processing."""
    return _is_umi_episode(state, _settings())


def _apply_runtime_env(settings: dict[str, Any]) -> None:
    threads = settings.get("video", {}).get("ffmpeg_threads")
    if threads is None:
        os.environ.pop("UMI_FFMPEG_THREADS", None)
    else:
        os.environ["UMI_FFMPEG_THREADS"] = str(threads)
    preset = settings.get("video", {}).get("preprocess_preset")
    if preset in (None, ""):
        os.environ.pop("UMI_FFMPEG_PRESET", None)
    else:
        os.environ["UMI_FFMPEG_PRESET"] = str(preset)


def _load_umi_modules(load_executability: bool = False) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    umi_root = repo_root / "DataProcessUMI"
    if not umi_root.is_dir():
        raise FileNotFoundError(f"DataProcessUMI directory not found: {umi_root}")
    rels = ["assessment", "preprocess", "transform"]
    if load_executability:
        rels.extend(["solve", "executability"])
    for rel in rels:
        path = umi_root / rel
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    modules = {
        "vrd": importlib.import_module("validate_raw_data"),
        "sa": importlib.import_module("smooth_assessment"),
        "pp": importlib.import_module("preprocess_trajectory"),
        "tf": importlib.import_module("transform_episode_w_world_base"),
    }
    if load_executability:
        modules["se"] = importlib.import_module("solve_executability")
        modules["robots"] = importlib.import_module("robots")
    return modules


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


def _trajectory_gate(sa: Any, episode_dir: Path, smooth_config: dict[str, Any],
                     settings: dict[str, Any]) -> dict[str, Any]:
    result = sa.assess_episode(episode_dir, smooth_config, settings["fps"])
    if not result.get("ok"):
        return {
            "enabled": True,
            "status": "reject",
            "label": None,
            "label_zh": None,
            "reason": result.get("error") or "trajectory assessment failed",
            "details": result,
        }
    label = result.get("trajectory_label") or {}
    code = label.get("code")
    passed = code in set(settings["trajectory_pass_labels"])
    status = "pass" if passed else settings["trajectory_nonpass_status"]
    reason = (
        f"trajectory label '{code}' is allowed by strict gate"
        if passed else
        f"trajectory label '{code}' is not in allowed labels: "
        + ", ".join(settings["trajectory_pass_labels"])
    )
    return {
        "enabled": True,
        "status": status,
        "label": code,
        "label_zh": label.get("name_zh"),
        "reason": reason,
        "frame_count": result.get("frame_count"),
        "duration_s": result.get("duration_s"),
        "dropped_frames": result.get("dropped_frames"),
        "details": {
            "trajectory_label": label,
            "devices": result.get("devices"),
        },
    }


def _run_executability(modules: dict[str, Any], settings: dict[str, Any],
                       episode_dir: Path, outdir: Path) -> dict[str, Any]:
    se = modules["se"]
    ik = settings["ik"]
    argv = [
        "--episode", str(episode_dir),
        "--outdir", str(outdir),
        "--arm", ik["arm"],
        "--source", ik["source"],
        "--no-transform",
        "--max-points", str(ik["max_points"]),
        "--min-segment", str(ik["min_segment"]),
        "--jobs", str(ik["jobs"]),
        "--samples", str(ik["samples"]),
    ]
    if ik["robots"]:
        argv += ["--robots"] + [str(robot) for robot in ik["robots"]]
    if ik["extra_args"]:
        argv += shlex.split(ik["extra_args"])
    rc = se.main(argv)
    summary_path = outdir / "summary.json"
    summary = None
    if summary_path.exists():
        with summary_path.open("r", encoding="utf-8") as file_obj:
            summary = json.load(file_obj)
    if rc not in (0, 1):
        raise RuntimeError(f"executability solver returned {rc}")
    return {
        "ran": True,
        "return_code": int(rc),
        "executable": _summary_has_executable(summary or {}),
        "summary_path": str(summary_path),
        "outdir": str(outdir),
        "source": ik["source"],
        "arm": ik["arm"],
        "robots": ik["robots"],
        "summary": summary,
    }


def _summary_has_executable(summary: dict[str, Any]) -> bool:
    for arm_result in (summary.get("results") or {}).values():
        for robot_result in (arm_result.get("robots") or {}).values():
            if robot_result.get("executable"):
                return True
    return False


def _executability_section(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "ran": True,
        "return_code": record.get("return_code"),
        "executable": record.get("executable"),
        "summary_path": record.get("summary_path"),
        "outdir": record.get("outdir"),
        "source": record.get("source"),
        "arm": record.get("arm"),
        "robots": record.get("robots"),
        "summary": record.get("summary"),
    }


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
    exec_record: dict | None,
    trajectory_gate: dict | None,
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
        "trajectory_gate": trajectory_gate,
        "assessment": _assessment_section(assess, gate),
        "classification": {
            "label": pre_record.get("label") if pre_record else None,
            "label_zh": pre_record.get("label_zh") if pre_record else None,
            "category": pre_record.get("category") if pre_record else None,
        },
        "smoothing": _smoothing_section(pre_record) if pre_record else None,
        "transform": tf_record,
        "executability": _executability_section(exec_record),
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
