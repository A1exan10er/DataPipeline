#!/usr/bin/env python3
"""程序二：根据求解器，把 TCP 轨迹解算成关节序列。

逐点逆运动学（沿轨迹热启动保证构型连续），输出每个 TCP 位姿对应的关节角，
并附 IK 是否收敛及位姿残差。输出关节序列 CSV。

用法：
  python tcp_to_joints.py --robot ur5e --input traj.csv --rot quat --out joints.csv
  python tcp_to_joints.py --robot aloha_piper --input traj.csv --rot rpy \
                          --time-col t --jobs 4 --only-reachable

输出列：index, t, <joint1..n>, ik_ok, pos_err_mm, rot_err_deg
单位：关节角默认弧度（--deg 输出角度）。
"""
from __future__ import annotations
import argparse
import csv
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robots                              # noqa: E402
from core import Thresholds               # noqa: E402
from io_poses import read_poses           # noqa: E402
from batch import validate                # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="TCP 轨迹 -> 关节序列（Pinocchio 逆运动学）")
    ap.add_argument("--robot", required=True, choices=robots.list_robots())
    ap.add_argument("--input", required=True, help="TCP 位姿 CSV")
    ap.add_argument("--rot", default="quat", choices=["quat", "rpy"],
                    help="姿态格式：quat=qx qy qz qw；rpy=roll pitch yaw(弧度)")
    ap.add_argument("--time-col", default=None, help="时间列名（透传到输出）")
    ap.add_argument("--out", default="joints.csv", help="关节序列 CSV")
    ap.add_argument("--jobs", type=int, default=1, help="并行进程数（连续分块）")
    ap.add_argument("--restarts", type=int, default=4, help="段首 IK 随机重启次数")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pos-tol-mm", type=float, default=1.0, help="IK 位置收敛阈值")
    ap.add_argument("--rot-tol-deg", type=float, default=0.5, help="IK 姿态收敛阈值")
    ap.add_argument("--deg", action="store_true", help="关节角以角度输出（默认弧度）")
    ap.add_argument("--only-reachable", action="store_true",
                    help="仅输出 IK 收敛的点（默认全部输出，不收敛点关节仍为最优近似解）")
    args = ap.parse_args(argv)

    th = Thresholds(pos_tol_mm=args.pos_tol_mm, rot_tol_deg=args.rot_tol_deg)
    poses, times = read_poses(args.input, args.rot, args.time_col)
    if not poses:
        print("没有读到位姿", file=sys.stderr)
        return 2

    # 复用批量求解器；这里只取每点的关节解与 IK 收敛信息。
    results = validate(args.robot, poses, times, th, jobs=args.jobs,
                       restarts=args.restarts, seed=args.seed)

    jnames = robots.load(args.robot).joint_names
    scale = np.degrees if args.deg else (lambda x: x)

    n_ok = 0
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "t"] + jnames + ["ik_ok", "pos_err_mm", "rot_err_deg"])
        for r in results:
            if args.only_reachable and not r.ik_ok:
                continue
            n_ok += int(r.ik_ok)
            q = [round(float(x), 6) for x in scale(np.array(r.q))]
            w.writerow([r.index, round(r.t, 6)] + q +
                       [int(r.ik_ok), round(r.pos_err_mm, 5), round(r.rot_err_deg, 5)])

    print(f"机械臂: {args.robot}  关节: {jnames}")
    print(f"求解: {sum(r.ik_ok for r in results)}/{len(results)} 点 IK 收敛")
    print(f"关节序列已写出: {args.out}  (单位: {'deg' if args.deg else 'rad'})")
    return 0 if all(r.ik_ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
