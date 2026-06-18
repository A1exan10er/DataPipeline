#!/usr/bin/env python3
"""One-click data pipeline: assessment -> preprocess -> transform [-> executability].

Each discovered episode is streamed through all enabled stages end to end before
the next episode is started -- the first episode reaches ``transform`` (and the
optional executability stage) without waiting for the rest of the input to be
assessed. Stages run in-process per episode, so there is no batch barrier
between stages. This produces a single, clean output tree:

    <output-root>/
      data/                                # final usable data only
        <class>_w_world_base/
          episode_XXXX/ ...                # smoothed + world-base transformed
      report/                              # one report per episode + a global one
        <class>/
          episode_XXXX.json                # combined: assessment + smoothing + transform
          episode_XXXX/executability/      # optional IK/executability outputs
        pipeline_report.json               # global run summary
      .work/                               # intermediates (removed unless --keep-intermediate)

Stages
------
1. **assessment** (``assessment/validate_raw_data.py``) -- validates the raw
   episode and records the per-check verdict. Informational: it does not gate
   the pipeline (a "incorrect" episode is still processed), but its result is
   folded into the per-episode report.
2. **preprocess** (``preprocess/preprocess_trajectory.py``) -- classifies the
   eef_pose trajectory (smooth / recoverable / middle_* / unrecoverable),
   interpolates recoverable jumps, crops head/tail unrecoverable spans, or
   **rejects** an unsalvageable episode. Only non-rejected episodes continue.
3. **transform** (``transform/transform_episode_w_world_base.py``) -- maps the
   cleaned episode into the world-base EEF frame and flips the wrist videos.
   Output class directories get a ``_w_world_base`` suffix (configurable).
4. **executability** (optional, ``executability/solve_executability.py``) --
   runs episode-level IK/executability solving on the transformed episode,
   writes per-arm/per-robot joints/reports, and folds the summary into the
   combined episode report.

Each episode's combined report records its step-2 classification, the smoothing
operations performed, the step-3 transform record, and the optional
executability summary. The global report summarises how many episodes were
processed, how many produced usable output, and -- for every episode -- whether
it passed, what was done, and (if it did not pass) why.
"""

import argparse
import json
import shlex
import shutil
import sys
import traceback
from pathlib import Path

_PIPE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PIPE_DIR.parent
_ASSESS_DIR = _DATA_DIR / "assessment"
_PRE_DIR = _DATA_DIR / "preprocess"
_TF_DIR = _DATA_DIR / "transform"
_EXEC_DIR = _DATA_DIR / "executability"
for _p in (_ASSESS_DIR, _PRE_DIR, _TF_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import validate_raw_data as vrd  # noqa: E402
import smooth_assessment as sa  # noqa: E402
import preprocess_trajectory as pp  # noqa: E402
import transform_episode_w_world_base as tf  # noqa: E402

DEFAULT_SUFFIX = "_w_world_base"
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run assessment -> preprocess -> transform over all episodes "
                    "in one pass, emitting data/ + report/ under the output root.")
    parser.add_argument("input_path_arg", nargs="?", type=Path,
                        help="Episode_XXX directory, class directory, or a root "
                             "containing class/episode dirs (recursively discovered).")
    parser.add_argument("-i", "--input-path", type=Path, help="Same as positional input.")
    parser.add_argument("-o", "--output-root", type=Path, default=Path("pipeline_out"),
                        help="Output root (default: pipeline_out). Holds data/ and report/.")
    parser.add_argument("--suffix", default=DEFAULT_SUFFIX,
                        help=f"Suffix appended to each output class name (default: {DEFAULT_SUFFIX}).")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing output root if present.")
    parser.add_argument("--skip-assessment", action="store_true",
                        help="Skip stage 1 (validation). Reports then omit the assessment section.")
    parser.add_argument("--keep-intermediate", action="store_true",
                        help="Keep the .work/ intermediates (assessment reports + cleaned episodes).")
    parser.add_argument("--no-video", action="store_true",
                        help="Testing only: skip video work in preprocess (breaks transform's "
                             "wrist-flip on cropped episodes).")
    parser.add_argument("--fps", type=float, help="Override FPS (default: metadata fps_config or 30).")
    parser.add_argument("--preprocess-config", type=Path, help="preprocess_config.json override.")
    parser.add_argument("--smooth-config", type=Path, help="smooth_assessment_config.json override.")
    parser.add_argument("--transform-config", type=Path, help="ee_trajectory_config.json override.")
    parser.add_argument("--assessment-args", default="",
                        help="Extra space-separated args forwarded to validate_raw_data.py "
                             "(e.g. \"--skip-focus --skip-motion\").")
    parser.add_argument("--run-executability", action="store_true",
                        help="After transform, run episode-level IK/executability solving "
                             "and attach its summary to the reports.")
    parser.add_argument("--ik-robots", nargs="*", default=None,
                        help="Robot subset for --run-executability (default: all registered robots).")
    parser.add_argument("--ik-arm", default="both", choices=["left", "right", "both"],
                        help="Arm(s) to solve for --run-executability (default: both).")
    parser.add_argument("--ik-source", default="action", choices=["action", "state"],
                        help="Use actions.eef_pose or observation.state.eef_pose for IK "
                             "(default: action).")
    parser.add_argument("--ik-max-points", type=int, default=200,
                        help="Downsample executability validation to at most N points "
                             "(0 disables downsampling; default: 200).")
    parser.add_argument("--ik-min-segment", type=int, default=5,
                        help="Minimum continuous executable segment length, in sampled points "
                             "(default: 5).")
    parser.add_argument("--ik-jobs", type=int, default=1,
                        help="Parallel workers passed to executability solving.")
    parser.add_argument("--ik-samples", type=int, default=80000,
                        help="Workspace samples per robot for executability solving.")
    parser.add_argument("--ik-extra-args", default="",
                        help="Extra args forwarded to executability/solve_executability.py.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Stage 1: assessment (in-process, one episode at a time)
# ---------------------------------------------------------------------------


def build_assessment_args(extra_args, fps):
    """Build the ``validate_raw_data`` args namespace once for the whole run.

    ``extra_args`` is the (possibly empty) ``--assessment-args`` string forwarded
    verbatim to validate_raw_data's own parser; the pipeline ``--fps`` (if any)
    overrides whatever it resolves. Config sections and the OpenCV/NumPy
    dependency check are resolved up front -- exactly as validate_raw_data's own
    ``main`` does -- so each per-episode call is pure work with no setup.
    """
    argv = extra_args.split() if extra_args else []
    assess_args = vrd.parse_args(argv)
    if fps is not None:
        assess_args.fps = fps
    vrd.apply_validate_config(assess_args)
    vrd.require_check_dependencies(assess_args)
    return assess_args


def assess_episode(assess_args, episode_dir, class_name, generated_at):
    """Validate one episode in-process and return its report payload.

    The payload has the same shape as the ``.validation.json`` that
    validate_raw_data.py writes, so it feeds straight into
    :func:`assessment_gate` / :func:`assessment_section`. Returns ``None`` if
    validation raises -- assessment is informational and never aborts the run.
    """
    try:
        result = vrd.validate_episode(episode_dir, assess_args)
        return vrd.episode_report_payload(class_name, result, assess_args, generated_at)
    except Exception as exc:  # noqa: BLE001 - a bad assessment must not gate the episode
        print(f"  (assessment failed for {episode_dir.name}: "
              f"{type(exc).__name__}: {exc}; continuing without it)")
        return None


def assessment_section(report, gate=None):
    """Condense a validation.json into the per-episode combined report."""
    if report is None:
        return None
    section = {
        "correct": report.get("correct"),
        "result": report.get("result"),
        "checks_run": report.get("checks_run"),
        "source_report": report.get("type"),
    }
    if gate is not None:
        section["gate"] = {"blocked": gate[0], "blocking_problems": gate[1],
                           "tolerated_problems": gate[2]}
    return section


# Video problems that are a *frame-drop* artefact (掉帧): dropped / duplicated /
# count-mismatched frames. These are tolerated -- the episode still proceeds.
# Everything NOT listed here (defocus, mislabel, L/R swap, missing files,
# non-monotonic timestamps, any action/pose problem) blocks the episode.
TOLERABLE_VIDEO_PROBLEMS = frozenset({
    "video_frame_count_mismatch",
    "missing_timestamps",
    "duplicate_frames_exceed_thresholds",
})


def assessment_gate(report):
    """Decide whether an assessment result blocks the episode.

    Policy: a **gripper** problem is tolerated; a **video frame-drop** problem is
    tolerated; anything else (other video problems, any action/pose problem)
    blocks the episode -- it is dropped with the reason recorded.

    Returns ``(blocked, blocking_problems, tolerated_problems)``. When no report
    exists (assessment skipped/failed), nothing is blocked.
    """
    if report is None:
        return False, [], []
    info = report.get("info") or {}
    blocking, tolerated = [], []

    video = info.get("video") or {}
    if video and not video.get("correct", True):
        # Per-stream problems (defocus, mislabel, duplicates, count mismatch...).
        for stream in video.get("streams", []):
            for p in stream.get("problems", []):
                tag = f"video:{stream.get('key')}:{p}"
                (tolerated if p in TOLERABLE_VIDEO_PROBLEMS else blocking).append(tag)
        # Video-level problems (e.g. wrist_view_lr_swap from motion check).
        for p in video.get("problems", []) or []:
            tag = f"video:{p}"
            (tolerated if p in TOLERABLE_VIDEO_PROBLEMS else blocking).append(tag)

    # Gripper: always tolerated, never blocks. Record the specific problem(s)
    # -- per-side mapping verdict or the load error -- so the report says what
    # was wrong instead of a bare "incorrect".
    gripper = info.get("gripper") or {}
    if gripper and not gripper.get("correct", True):
        grip_tags = []
        if gripper.get("error"):
            grip_tags.append(f"gripper:{gripper['error']}")
        for side, result in (gripper.get("sides") or {}).items():
            if result and not result.get("correct"):
                grip_tags.append(
                    f"gripper:{side}:{result.get('problem_type') or result.get('type')}")
        tolerated.extend(grip_tags or ["gripper:incorrect"])

    # Action / pose: any problem blocks.
    action = info.get("action") or {}
    if action and not action.get("correct", True):
        probs = action.get("problems") or ["incorrect"]
        blocking.extend(f"action:{p}" for p in probs)

    return (len(blocking) > 0), blocking, tolerated


# ---------------------------------------------------------------------------
# Combined per-episode report assembly
# ---------------------------------------------------------------------------


def smoothing_section(record):
    return {
        "operations": record.get("operations"),
        "quality": record.get("quality"),
        "interpolated": record.get("interpolated"),
        "crop": record.get("crop"),
        "original_frames": record.get("original_frames"),
        "kept_frames": record.get("kept_frames"),
    }


def executability_section(record):
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


def combined_report_payload(class_name, episode_name, status, reason, failed_stage,
                            assess, gate, pre_record, tf_record, exec_record,
                            data_path, generated_at):
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "pipeline_episode_report",
        "generated_at": generated_at,
        "class_name": class_name,
        "episode": episode_name,
        "status": status,            # passed | rejected | error
        "reason": reason,            # why it did not pass (None when passed)
        "failed_stage": failed_stage,  # assessment | preprocess | transform | None
        "data_path": data_path,      # final transformed episode dir (None if not produced)
        "assessment": assessment_section(assess, gate),
        "classification": {
            "label": pre_record.get("label") if pre_record else None,
            "label_zh": pre_record.get("label_zh") if pre_record else None,
            "category": pre_record.get("category") if pre_record else None,
        },
        "smoothing": smoothing_section(pre_record) if pre_record else None,
        "transform": tf_record,
        "executability": executability_section(exec_record),
    }


def _summary_has_executable(summary):
    for arm_result in (summary.get("results") or {}).values():
        for robot_result in (arm_result.get("robots") or {}).values():
            if robot_result.get("executable"):
                return True
    return False


def run_executability(args, episode_dir, outdir):
    """Run the optional episode-level IK/executability stage.

    The episode has already been transformed into the world-base EEF frame by
    stage 3, so the executability reader is called with ``--no-transform`` to
    avoid applying the tracker->world transform twice.
    """
    if str(_EXEC_DIR) not in sys.path:
        sys.path.insert(0, str(_EXEC_DIR))
    try:
        import solve_executability as se  # noqa: E402
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "executability dependencies are unavailable; install pin/scipy and "
            "make sure executability/ and solve/ are present"
        ) from exc

    argv = [
        "--episode", str(episode_dir),
        "--outdir", str(outdir),
        "--arm", args.ik_arm,
        "--source", args.ik_source,
        "--no-transform",
        "--max-points", str(args.ik_max_points),
        "--min-segment", str(args.ik_min_segment),
        "--jobs", str(args.ik_jobs),
        "--samples", str(args.ik_samples),
    ]
    if args.ik_robots:
        argv += ["--robots"] + list(args.ik_robots)
    if args.ik_extra_args:
        argv += shlex.split(args.ik_extra_args)

    rc = se.main(argv)
    summary_path = outdir / "summary.json"
    summary = None
    if summary_path.exists():
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
    if rc not in (0, 1):
        raise RuntimeError(f"executability solver returned {rc}")
    return {
        "return_code": int(rc),
        "executable": _summary_has_executable(summary or {}),
        "summary_path": str(summary_path),
        "outdir": str(outdir),
        "source": args.ik_source,
        "arm": args.ik_arm,
        "robots": args.ik_robots,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main(argv=None):
    args = parse_args(argv)
    input_path = args.input_path or args.input_path_arg
    if input_path is None:
        raise ValueError(
            "Input path is required. Expected an episode_XXX directory, a class "
            "directory, or a root containing them.")
    input_path = input_path.resolve()

    output_root = args.output_root.resolve()
    if output_root.exists() and args.overwrite:
        shutil.rmtree(output_root)
    data_root = output_root / "data"
    report_root = output_root / "report"
    work_dir = output_root / ".work"
    pre_root = work_dir / "preprocess"
    for d in (data_root, report_root, work_dir, pre_root):
        d.mkdir(parents=True, exist_ok=True)

    # Configs for the in-process stages.
    pre_config = pp.load_preprocess_config(args.preprocess_config)
    smooth_config = sa.load_config(args.smooth_config)
    tf_config = tf.load_config(args.transform_config)

    episode_dirs = vrd.find_episode_dirs(input_path)
    generated_at = vrd.now_utc_iso()
    print(f"Pipeline over {len(episode_dirs)} episode(s) from: {input_path}")
    print(f"Output root: {output_root}")

    # Stage 1 setup: build the assessment args once (config + dependency check
    # resolved up front). Each episode is then validated in-process inside the
    # loop, so it flows assessment -> preprocess -> transform without waiting for
    # the rest of the input to be assessed.
    assess_args = None
    if not args.skip_assessment:
        assess_args = build_assessment_args(args.assessment_args, args.fps)
        assess_report_root = work_dir / "assessment"
    else:
        print("[1/3] assessment: skipped")

    stage_names = ["assessment", "preprocess", "transform"]
    if args.run_executability:
        stage_names.append("executability")
    print(f"[pipeline] {' -> '.join(stage_names)}, per episode: "
          f"{len(episode_dirs)} episode(s)")
    episodes_summary = []
    by_category = {}
    by_label = {}

    for ep in episode_dirs:
        rel_subdir, class_name = vrd.episode_output_layout(input_path, ep)
        rel_subdir = Path(rel_subdir)
        ep_name = ep.name

        # Stage 1: validate this episode in-process (informational, never gates
        # to "error"). Mirror the report under .work for --keep-intermediate.
        assess = None
        if assess_args is not None:
            assess = assess_episode(assess_args, ep, class_name, generated_at)
            if assess is not None:
                vrd.write_json_report(
                    assess_report_root / rel_subdir
                    / f"{ep_name}{vrd.EPISODE_REPORT_SUFFIX}", assess)
        gate = assessment_gate(assess)
        status, reason, data_path, failed_stage = "error", None, None, None
        pre_record, tf_record, exec_record = None, None, None

        if gate[0]:
            # Stage-1 gate: a non-tolerable assessment problem (anything beyond a
            # gripper issue or a video frame-drop). Drop the episode now; record
            # why, and do not run preprocess/transform.
            status = "rejected"
            failed_stage = "assessment"
            reason = "assessment failed (non frame-drop / non-gripper): " \
                     + ", ".join(gate[1])
        else:
            try:
                # Stage 2: preprocess (classify + smooth/crop or reject).
                cleaned_ep = pre_root / rel_subdir / ep_name
                pre_record = pp.process_episode(
                    ep, cleaned_ep, pre_config, smooth_config, args.fps,
                    write=True, do_video=not args.no_video, overwrite=True)

                label = pre_record.get("label")
                category = pre_record.get("category")
                if label:
                    by_label[label] = by_label.get(label, 0) + 1
                if category:
                    by_category[category] = by_category.get(category, 0) + 1

                if not pre_record.get("ok"):
                    # Rejected (unrecoverable / too short) or load error.
                    status = "rejected" if category == "reject" or pre_record.get("kept_frames") == 0 \
                        else "error"
                    failed_stage = "preprocess"
                    reason = pre_record.get("error") or \
                        f"preprocess produced no output ({category})"
                elif category == "reject":
                    status, failed_stage = "rejected", "preprocess"
                    reason = pre_record.get("error") or "unrecoverable trajectory"
                else:
                    # Stage 3: transform the cleaned episode into world-base frame.
                    out_class = f"{rel_subdir.name}{args.suffix}"
                    dest_ep = data_root / rel_subdir.parent / out_class / ep_name
                    tf_record = tf.transform_episode(cleaned_ep, dest_ep, tf_config)
                    if args.run_executability:
                        exec_out = report_root / rel_subdir / ep_name / "executability"
                        exec_record = run_executability(args, dest_ep, exec_out)
                    status, reason = "passed", None
                    data_path = str(dest_ep)
            except Exception as exc:  # noqa: BLE001 - one bad episode must not abort the run
                status = "error"
                if exec_record is None and tf_record is not None and args.run_executability:
                    failed_stage = "executability"
                else:
                    failed_stage = "transform" if pre_record and pre_record.get("ok") else "preprocess"
                reason = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()

        # Write the combined per-episode report.
        report_path = report_root / rel_subdir / f"{ep_name}.json"
        vrd.write_json_report(report_path, combined_report_payload(
            class_name, ep_name, status, reason, failed_stage, assess, gate,
            pre_record, tf_record, exec_record, data_path, generated_at))

        episodes_summary.append({
            "episode": ep_name,
            "class_name": class_name,
            "rel_subdir": str(rel_subdir),
            "status": status,
            "failed_stage": failed_stage,
            "reason": reason,
            "label": pre_record.get("label") if pre_record else None,
            "category": pre_record.get("category") if pre_record else None,
            "operations": pre_record.get("operations") if pre_record else None,
            "executability": {
                "ran": bool(exec_record),
                "executable": exec_record.get("executable") if exec_record else None,
                "summary": exec_record.get("summary_path") if exec_record else None,
            },
            "data_path": data_path,
            "report": str(report_path),
        })
        flag = {"passed": "OK", "rejected": "REJECT", "error": "ERROR"}[status]
        lbl = (pre_record.get("label") if pre_record else None) or "-"
        print(f"  [{flag}] {class_name}/{ep_name}: {lbl}"
              + (f" -> {reason}" if reason else ""))

    processed = len(episodes_summary)
    passed = sum(1 for e in episodes_summary if e["status"] == "passed")
    rejected = sum(1 for e in episodes_summary if e["status"] == "rejected")
    errored = sum(1 for e in episodes_summary if e["status"] == "error")
    dropped_by_stage = {}
    for e in episodes_summary:
        if e["status"] in ("rejected", "error") and e["failed_stage"]:
            dropped_by_stage[e["failed_stage"]] = dropped_by_stage.get(e["failed_stage"], 0) + 1

    global_report = {
        "schema_version": SCHEMA_VERSION,
        "type": "pipeline_global_report",
        "generated_at": generated_at,
        "input_path": str(input_path),
        "output_root": str(output_root),
        "data_root": str(data_root),
        "suffix": args.suffix,
        "stages": stage_names,
        "assessment_run": assess_args is not None,
        "executability_run": bool(args.run_executability),
        "totals": {
            "processed": processed,
            "passed": passed,        # produced usable transformed data
            "rejected": rejected,    # dropped (assessment gate or preprocess reject)
            "error": errored,        # failed in some stage (with reason)
        },
        "dropped_by_stage": dropped_by_stage,
        "by_label": by_label,
        "by_category": by_category,
        "episodes": episodes_summary,
    }
    global_path = report_root / "pipeline_report.json"
    vrd.write_json_report(global_path, global_report)

    if not args.keep_intermediate:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\nDone. processed={processed} passed={passed} "
          f"rejected={rejected} error={errored}")
    print(f"  data:   {data_root}")
    print(f"  report: {report_root} (global: {global_path})")
    if args.keep_intermediate:
        print(f"  intermediates kept under: {work_dir}")
    return global_report


if __name__ == "__main__":
    main()
