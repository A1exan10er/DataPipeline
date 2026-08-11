from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
QA_PIPELINE_ROOT = PROJECT_ROOT / "QA_Pipeline"
if str(QA_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(QA_PIPELINE_ROOT))

from QA_Pipeline.scripts.pipeline import phase3_timestamp


def assert_equal(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"PASS {label}: {actual!r}")


def timestamps_for_actual_fps(actual_fps: float, frame_count: int = 31) -> list[float]:
    interval_ms = 1000.0 / actual_fps
    return [index * interval_ms for index in range(frame_count)]


def state_for_modality(modality: str) -> SimpleNamespace:
    return SimpleNamespace(
        episode_path=Path("/tmp/episode_0000"),
        metadata={
            "modalities": {
                modality: {
                    "hz": 30,
                }
            }
        },
    )


def run_threshold_tests() -> None:
    normal_state = state_for_modality("observation.image.left_wrist_view")
    tactile_state = state_for_modality("observation.image.left_wrist_left_tactile")

    normal_12_percent_loss = phase3_timestamp._check_actual_frequency(
        normal_state,
        "observation.image.left_wrist_view",
        timestamps_for_actual_fps(26.4),
    )
    assert_equal(len(normal_12_percent_loss), 1, "normal video 12 percent FPS loss fails")
    assert_equal(
        normal_12_percent_loss[0].details["threshold"],
        0.10,
        "normal video FPS loss threshold is 10 percent",
    )

    tactile_12_percent_loss = phase3_timestamp._check_actual_frequency(
        tactile_state,
        "observation.image.left_wrist_left_tactile",
        timestamps_for_actual_fps(26.4),
    )
    assert_equal(len(tactile_12_percent_loss), 0, "tactile video 12 percent FPS loss passes")

    tactile_16_percent_loss = phase3_timestamp._check_actual_frequency(
        tactile_state,
        "observation.image.left_wrist_left_tactile",
        timestamps_for_actual_fps(25.2),
    )
    assert_equal(len(tactile_16_percent_loss), 1, "tactile video 16 percent FPS loss fails")
    assert_equal(
        tactile_16_percent_loss[0].details["threshold"],
        0.15,
        "tactile video FPS loss threshold is 15 percent",
    )


if __name__ == "__main__":
    run_threshold_tests()
