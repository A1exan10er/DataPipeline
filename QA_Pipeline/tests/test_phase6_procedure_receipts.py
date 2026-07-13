from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.pipeline import phase6_umi_processing as phase6
from QA_Pipeline.scripts.pipeline.qa_core import EpisodeState
from DataProcessUMI.transform import transform_episode_w_world_base as umi_transform


ROTATIONS = {
    "observation.image.left_wrist_view": 180,
    "observation.image.right_wrist_view": 180,
}


def _settings(profile: str) -> dict:
    return {
        "profile": profile,
        "enabled": True,
        "suffix": "_w_world_base",
        "video_only_suffix": "_video_aligned",
        "overwrite": True,
        "skip_assessment": True,
        "trajectory_first_gate": False,
        "run_executability": False,
        "video": {
            "orientation_correction": ROTATIONS,
            "output_mode": "derived_copy",
            "sidecar_filename": "video_rotated_180.mp4",
        },
        "umi_tokens": ["umi"],
        "required_modalities": [],
    }


def _state(source: Path) -> EpisodeState:
    return EpisodeState(
        episode_path=source,
        task="test_umi",
        date="",
        operator="",
        robot="umi",
        controller="",
        metadata={"robot": "umi"},
    )


def _make_episode(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "metadata.json").write_text('{"robot": "umi"}\n', encoding="utf-8")
    (path / "actions.eef_pose").mkdir()
    (path / "actions.eef_pose" / "data.csv").write_text("timestamp_ms\n0\n", encoding="utf-8")
    for stream in ROTATIONS:
        stream_dir = path / stream
        stream_dir.mkdir()
        (stream_dir / "video.mp4").write_bytes(b"raw-video")
        (stream_dir / "timestamps.csv").write_text("timestamp_ms\n0\n", encoding="utf-8")


def test_receipt_requires_matching_config_and_outputs(tmp_path: Path) -> None:
    episode = tmp_path / "episode_0001"
    _make_episode(episode)
    config = {"rotations_degrees": ROTATIONS}
    outputs = phase6._video_output_paths(ROTATIONS)

    phase6._record_procedure(
        episode,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        config,
        outputs,
        episode,
    )

    assert phase6._procedure_complete(
        episode,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        config,
        outputs,
    )
    assert not phase6._procedure_complete(
        episode,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        {"rotations_degrees": {**ROTATIONS, "observation.image.left_wrist_view": 0}},
        outputs,
    )
    (episode / outputs[0]).unlink()
    assert not phase6._procedure_complete(
        episode,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        config,
        outputs,
    )


def test_receipt_restores_read_only_episode_permissions(tmp_path: Path) -> None:
    episode = tmp_path / "episode_0001"
    _make_episode(episode)
    metadata_path = episode / "metadata.json"
    metadata_path.chmod(0o444)
    episode.chmod(0o555)

    phase6._record_procedure(
        episode,
        phase6.VIDEO_ORIENTATION_SIDECAR_PROCEDURE,
        {
            "rotations_degrees": ROTATIONS,
            "output": "sidecar",
            "sidecar_filename": "video_rotated_180.mp4",
        },
        [],
        episode,
    )

    assert episode.stat().st_mode & 0o777 == 0o555
    assert metadata_path.stat().st_mode & 0o777 == 0o444
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert phase6.VIDEO_ORIENTATION_SIDECAR_PROCEDURE in metadata["data_pipeline"]["completed_procedures"]


def test_video_only_is_incremental_and_does_not_touch_csv(tmp_path: Path) -> None:
    source = tmp_path / "source" / "episode_0001"
    _make_episode(source)
    state = _state(source)
    output_root = tmp_path / "output"
    calls = []

    class FakeTransform:
        @staticmethod
        def rotate_wrist_videos(episode: Path, rotations: dict[str, int]) -> list[str]:
            calls.append(dict(rotations))
            rotated = []
            for stream in rotations:
                path = episode / stream / "video.mp4"
                path.write_bytes(path.read_bytes() + b"-rotated-180")
                rotated.append(f"{stream}/video.mp4")
            return rotated

        @staticmethod
        def recompute_checksum_manifest(_episode: Path) -> bool:
            return True

    fake_vrd = SimpleNamespace(
        now_utc_iso=lambda: "2026-07-10T00:00:00Z",
        write_json_report=lambda path, payload: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.write_text(json.dumps(payload), encoding="utf-8"),
        ),
    )
    context = {
        "modules": {"vrd": fake_vrd, "tf": FakeTransform},
        "output_root": output_root,
        "data_root": output_root / "data",
        "report_root": output_root / "report",
    }
    settings = _settings("video_only")

    first = phase6._process_video_only_episode(state, context, settings)
    destination = Path(first["data_path"])
    csv_before = (destination / "actions.eef_pose" / "data.csv").read_bytes()
    second = phase6._process_video_only_episode(state, context, settings)

    assert len(calls) == 1
    assert first["executed_procedures"] == [phase6.VIDEO_ORIENTATION_PROCEDURE]
    assert second["skipped_procedures"] == [phase6.VIDEO_ORIENTATION_PROCEDURE]
    assert (destination / "actions.eef_pose" / "data.csv").read_bytes() == csv_before
    metadata = json.loads((destination / "metadata.json").read_text(encoding="utf-8"))
    assert phase6.VIDEO_ORIENTATION_PROCEDURE in metadata["data_pipeline"]["completed_procedures"]


def test_video_only_sidecars_preserve_original_videos(tmp_path: Path) -> None:
    source = tmp_path / "source" / "episode_0001"
    _make_episode(source)
    state = _state(source)
    output_root = tmp_path / "output"
    original_bytes = {
        stream: (source / stream / "video.mp4").read_bytes()
        for stream in ROTATIONS
    }

    class FakeTransform:
        @staticmethod
        def rotate_wrist_videos_to_sidecars(
            episode: Path,
            rotations: dict[str, int],
            output_filename: str,
        ) -> list[str]:
            outputs = []
            for stream in rotations:
                sidecar = episode / stream / output_filename
                sidecar.write_bytes(b"rotated-sidecar")
                outputs.append(f"{stream}/{output_filename}")
            return outputs

        @staticmethod
        def recompute_checksum_manifest(_episode: Path) -> bool:
            return True

    fake_vrd = SimpleNamespace(
        now_utc_iso=lambda: "2026-07-10T00:00:00Z",
        write_json_report=lambda path, payload: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.write_text(json.dumps(payload), encoding="utf-8"),
        ),
    )
    context = {
        "modules": {"vrd": fake_vrd, "tf": FakeTransform},
        "output_root": output_root,
        "data_root": output_root / "data",
        "report_root": output_root / "report",
    }
    settings = _settings("video_only")
    settings["video"]["output_mode"] = "sidecar"

    result = phase6._process_video_only_episode(state, context, settings)

    assert Path(result["data_path"]) == source
    for stream, before in original_bytes.items():
        assert (source / stream / "video.mp4").read_bytes() == before
        assert (source / stream / "video_rotated_180.mp4").read_bytes() == b"rotated-sidecar"
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    assert (
        phase6.VIDEO_ORIENTATION_SIDECAR_PROCEDURE
        in metadata["data_pipeline"]["completed_procedures"]
    )


def test_sidecar_writer_restores_read_only_camera_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    episode = tmp_path / "episode_0001"
    _make_episode(episode)
    for stream in ROTATIONS:
        (episode / stream).chmod(0o555)

    def fake_transcode(_source, output, **_kwargs) -> None:
        Path(output).write_bytes(b"rotated")

    monkeypatch.setattr(umi_transform, "run_video_transcode", fake_transcode)
    outputs = umi_transform.rotate_wrist_videos_to_sidecars(episode, ROTATIONS)

    assert len(outputs) == 2
    for stream in ROTATIONS:
        camera_dir = episode / stream
        assert camera_dir.stat().st_mode & 0o777 == 0o555
        assert (camera_dir / "video_rotated_180.mp4").read_bytes() == b"rotated"


def test_sidecar_writer_does_not_publish_partial_pair_on_transcode_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    episode = tmp_path / "episode_0001"
    _make_episode(episode)
    for stream in ROTATIONS:
        (episode / stream / "video_rotated_180.mp4").write_bytes(b"previous-complete")
    calls = 0

    def interrupted_transcode(_source, output, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        Path(output).write_bytes(b"new-output")
        if calls == 2:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(umi_transform, "run_video_transcode", interrupted_transcode)

    try:
        umi_transform.rotate_wrist_videos_to_sidecars(episode, ROTATIONS)
    except RuntimeError as exc:
        assert str(exc) == "simulated interruption"
    else:
        raise AssertionError("expected simulated interruption")

    for stream in ROTATIONS:
        camera_dir = episode / stream
        assert (camera_dir / "video_rotated_180.mp4").read_bytes() == b"previous-complete"
        assert not list(camera_dir.glob(".video_rotated_180.tmp-*.mp4"))


def test_video_transcode_preserves_source_codec_and_stream_format(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("UMI_FFMPEG_THREADS", "2")
    monkeypatch.setenv("UMI_FFMPEG_PRESET", "ultrafast")
    source = tmp_path / "video.mp4"
    output = tmp_path / "video_rotated_180.mp4"
    source.write_bytes(b"source")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_name": "hevc",
                                "codec_tag_string": "hvc1",
                                "pix_fmt": "yuv420p",
                                "color_range": "tv",
                                "color_space": "bt709",
                            }
                        ]
                    }
                )
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(umi_transform.subprocess, "run", fake_run)

    umi_transform.run_video_transcode(
        source,
        output,
        rotation_degrees=180,
        replace_input=False,
    )

    ffmpeg = commands[-1]
    assert ffmpeg[0] == "ffmpeg"
    assert ffmpeg[ffmpeg.index("-c:v") + 1] == "libx265"
    assert ffmpeg[ffmpeg.index("-preset") + 1] == "ultrafast"
    assert ffmpeg[ffmpeg.index("-pix_fmt") + 1] == "yuv420p"
    assert ffmpeg[ffmpeg.index("-tag:v") + 1] == "hvc1"
    assert ffmpeg[ffmpeg.index("-color_range") + 1] == "tv"
    assert ffmpeg[ffmpeg.index("-colorspace") + 1] == "bt709"
    assert ffmpeg[ffmpeg.index("-x265-params") + 1] == "pools=2:log-level=error"
    assert "-an" not in ffmpeg
    assert ffmpeg[ffmpeg.index("-c:a") + 1] == "copy"


def test_checksum_writer_restores_read_only_manifest(tmp_path: Path) -> None:
    episode = tmp_path / "episode_0001"
    _make_episode(episode)
    manifest = episode / "checksums.sha256"
    manifest.write_text("old\n", encoding="utf-8")
    manifest.chmod(0o444)

    assert umi_transform.recompute_checksum_manifest(episode)

    assert manifest.stat().st_mode & 0o777 == 0o444
    assert "metadata.json" in manifest.read_text(encoding="utf-8")
    assert not list(episode.glob(".checksums.sha256.tmp-*"))


def test_full_profile_is_not_hidden_by_video_only_phase_completion(tmp_path: Path) -> None:
    source = tmp_path / "source" / "episode_0001"
    _make_episode(source)
    state = _state(source)
    phase6._record_procedure(
        source,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        {"rotations_degrees": ROTATIONS},
        phase6._video_output_paths(ROTATIONS),
        source,
    )
    state.phases_completed = [6]
    state.metrics = {
        "p6_umi_requested_procedures": [phase6.VIDEO_ORIENTATION_PROCEDURE],
        "p6_umi_data_path": str(source),
    }

    assert not phase6.needs_run(state, _settings("video_only"))
    assert phase6.needs_run(state, _settings("full"))


def test_full_profile_reuses_video_only_output(tmp_path: Path) -> None:
    source = tmp_path / "source" / "episode_0001"
    _make_episode(source)
    state = _state(source)
    output_root = tmp_path / "output"
    context = {"output_root": output_root, "data_root": output_root / "data"}
    settings = _settings("full")
    video_destination = phase6._profile_destination(state, context, settings, "video_only")
    _make_episode(video_destination)
    phase6._record_procedure(
        video_destination,
        phase6.VIDEO_ORIENTATION_PROCEDURE,
        phase6._procedure_config(
            phase6.VIDEO_ORIENTATION_PROCEDURE,
            settings,
            context,
        ),
        phase6._video_output_paths(ROTATIONS),
        source,
    )

    assert phase6._full_resume_source(state, context, settings) == video_destination


def test_realtime_environment_overrides_phase6_profile(monkeypatch) -> None:
    monkeypatch.setenv("QA_PHASE6_PROFILE", "video_only")
    monkeypatch.setenv("QA_PHASE6_VIDEO_OUTPUT_MODE", "sidecar")
    monkeypatch.setenv("QA_PHASE6_SIDECAR_FILENAME", "realtime_rotated.mp4")

    settings = phase6._settings()

    assert settings["profile"] == "video_only"
    assert settings["video"]["output_mode"] == "sidecar"
    assert settings["video"]["sidecar_filename"] == "realtime_rotated.mp4"
