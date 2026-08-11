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
    try:
        from dcs_sdk.config import get_section

        rabbitmq = get_section("rabbitmq")
        if not str(rabbitmq.get("url") or "").strip():
            raise ValueError("rabbitmq.url 为空")
    except Exception as exc:
        print(f"DCS 配置校验失败：{exc}", file=sys.stderr)
        return 1

    print(f"DCS 配置校验通过：{config_path}（rabbitmq.url 已配置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
