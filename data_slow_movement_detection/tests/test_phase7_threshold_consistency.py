from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STANDALONE_SCRIPT = (
    PROJECT_ROOT / "data_slow_movement_detection" / "scripts" / "detect_slow_movement.py"
)
QUALITY_RULES = PROJECT_ROOT / "QA_Pipeline" / "configs" / "quality_rules.json"


def load_standalone_module():
    spec = importlib.util.spec_from_file_location("standalone_slow_movement", STANDALONE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_standalone_defaults_match_phase7_core_thresholds() -> None:
    module = load_standalone_module()
    args = module.build_parser().parse_args([])
    configured = json.loads(QUALITY_RULES.read_text(encoding="utf-8"))["phase7_slow_motion"]

    assert args.window_s == configured["window_seconds"]
    assert args.min_speed_cm_s == configured["min_speed_cm_s"]
    assert args.max_speed_cm_s == configured["max_speed_cm_s"]
    assert args.merge_gap_s == configured["merge_gap_seconds"]
    assert args.min_duration_s == configured["min_duration_seconds"]
    assert args.min_segment_path_cm == configured["min_segment_path_cm"]
