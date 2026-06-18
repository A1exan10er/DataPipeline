#!/usr/bin/env python3
"""程序一：判定一段 TCP 轨迹是否可执行。

根据求解器逐点求逆运动学，并检查：IK 是否收敛（误差估计）、关节是否越限、
是否接近奇异、是否自碰撞、相邻点关节速度是否超限；综合给出 executable 判定
与 0~1 质量分。输出逐点报告 CSV + 汇总 JSON。

用法：
  python check_trajectory.py --robot ur5e --input traj.csv --rot quat --out report.csv
  python check_trajectory.py --robot franka_fr3v2 --input traj.csv --rot rpy \
                             --time-col t --jobs 4 --calibrate 200
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
from typing import List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robots                                            # noqa: E402
from core import Thresholds, failure_reason, PointResult  # noqa: E402
from io_poses import read_poses                          # noqa: E402
from batch import validate                               # noqa: E402


def write_report(results: List[PointResult], th: Thresholds,
                 out_csv: str, out_json: Optional[str]):
    fields = list(results[0].row().keys()) + ["reason"]
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = r.row()
            row["reason"] = failure_reason(r, th)
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 5)
            w.writerow(row)

    n = len(results)
    ok = sum(r.executable for r in results)
    reasons: dict = {}
    first_fail = None
    for r in results:
        if not r.executable:
            rs = failure_reason(r, th)
            reasons[rs] = reasons.get(rs, 0) + 1
            if first_fail is None:
                first_fail = r.index
    summary = {
        "n_points": n,
        "n_executable": ok,
        "executable_ratio": round(ok / n, 4) if n else 0.0,
        "first_failure_index": first_fail,
        "failure_reasons": reasons,
        "worst_quality": round(min((r.quality for r in results), default=1.0), 4),
        "mean_quality": round(float(np.mean([r.quality for r in results])) if n else 1.0, 4),
        "max_pos_err_mm": round(max((r.pos_err_mm for r in results), default=0.0), 4),
        "max_rot_err_deg": round(max((r.rot_err_deg for r in results), default=0.0), 4),
        "min_clearance_mm": round(min((r.clearance_mm for r in results), default=0.0), 4),
        "min_sigma": round(min((r.sigma_min for r in results), default=0.0), 5),
    }
    if out_json:
        with open(out_json, "w") as f:
            json.dump(summary, f, indent=2)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="判定 TCP 轨迹是否可执行（Pinocchio + Coal）")
    ap.add_argument("--robot", required=True, choices=robots.list_robots())
    ap.add_argument("--input", required=True, help="TCP 位姿 CSV")
    ap.add_argument("--rot", default="quat", choices=["quat", "rpy"],
                    help="姿态格式：quat=qx qy qz qw；rpy=roll pitch yaw(弧度)")
    ap.add_argument("--time-col", default=None, help="时间列名（用于速度校验）")
    ap.add_argument("--out", default="report.csv", help="逐点报告 CSV")
    ap.add_argument("--summary", default=None, help="汇总 JSON（默认 <out>.summary.json）")
    ap.add_argument("--jobs", type=int, default=1, help="并行进程数（连续分块）")
    ap.add_argument("--restarts", type=int, default=4, help="段首 IK 随机重启次数")
    ap.add_argument("--calibrate", type=int, default=0,
                    help="采样 N 次自动屏蔽恒碰撞对（无 SRDF 的机型建议 200）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pos-tol-mm", type=float, default=1.0)
    ap.add_argument("--rot-tol-deg", type=float, default=0.5)
    ap.add_argument("--sigma-min", type=float, default=0.02)
    ap.add_argument("--clearance-mm", type=float, default=2.0)
    ap.add_argument("--vel-ratio", type=float, default=1.0)
    args = ap.parse_args(argv)

    th = Thresholds(args.pos_tol_mm, args.rot_tol_deg, args.sigma_min,
                    args.clearance_mm, args.vel_ratio)
    poses, times = read_poses(args.input, args.rot, args.time_col)
    if not poses:
        print("没有读到位姿", file=sys.stderr)
        return 2
    results = validate(args.robot, poses, times, th, jobs=args.jobs,
                       calibrate=args.calibrate, restarts=args.restarts, seed=args.seed)
    summary_path = args.summary or (args.out.rsplit(".", 1)[0] + ".summary.json")
    summary = write_report(results, th, args.out, summary_path)

    print(f"机械臂: {args.robot}  点数: {summary['n_points']}")
    print(f"可执行: {summary['n_executable']}/{summary['n_points']} "
          f"({summary['executable_ratio']*100:.1f}%)")
    if summary["failure_reasons"]:
        print("失败原因:", summary["failure_reasons"])
        print("首个失败点:", summary["first_failure_index"])
    print(f"质量: 最差 {summary['worst_quality']:.3f} / 平均 {summary['mean_quality']:.3f}")
    print(f"最大IK误差: {summary['max_pos_err_mm']:.3f}mm / "
          f"{summary['max_rot_err_deg']:.3f}deg  "
          f"最小间隙: {summary['min_clearance_mm']:.2f}mm  "
          f"最小奇异值: {summary['min_sigma']:.4f}")
    print(f"报告: {args.out}   汇总: {summary_path}")
    return 0 if summary["executable_ratio"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
