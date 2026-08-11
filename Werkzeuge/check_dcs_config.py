#!/usr/bin/env python3
"""部署切换前只读校验实时检测所需的生产 DCS 配置。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验实时检测所需的 DCS 配置。")
    parser.add_argument("--dc-root", required=True, type=Path)
    parser.add_argument("--qa-root", required=True, type=Path)
    parser.add_argument("--phases", default="1,2,3,7")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_value = os.environ.get("DCS_CONFIG_FILE", "").strip()
    if not config_value:
        print("DCS_CONFIG_FILE 未设置。", file=sys.stderr)
        return 1
    config_path = Path(config_value).expanduser()
    if not config_path.is_file():
        print(f"DCS 配置文件不存在或不可读：{config_path}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(args.dc_root))
    sys.path.insert(0, str(args.qa_root))
    try:
        from dcs_sdk.config import get_section

        rabbitmq = get_section("rabbitmq")
        if not str(rabbitmq.get("url") or "").strip():
            raise ValueError("rabbitmq.url 为空")

        from scripts.pipeline import (
            phase1_metadata,
            phase2_duration,
            phase3_timestamp,
            phase4_video,
            phase5_robot_state,
            phase6_umi_processing,
            phase7_standstill,
        )

        modules = {
            1: phase1_metadata,
            2: phase2_duration,
            3: phase3_timestamp,
            4: phase4_video,
            5: phase5_robot_state,
            6: phase6_umi_processing,
            7: phase7_standstill,
        }
        phases = {int(item.strip()) for item in args.phases.split(",") if item.strip()}
        unknown = sorted(phases - modules.keys())
        if unknown:
            raise ValueError(f"未知检测阶段：{unknown}")
        for phase in sorted(phases):
            validator = getattr(modules[phase], "validate_dependencies", None)
            if callable(validator):
                validator()
    except Exception as exc:
        print(f"生产配置或检测阶段依赖校验失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"生产预检通过：DCS={config_path}，rabbitmq.url 已配置，检测阶段={args.phases}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
