#!/usr/bin/env python3
"""生成一段可达的样例 TCP 轨迹（对某机型在关节空间做平滑摆动后 FK 得到 TCP 位姿）。
输出 quat 格式 CSV：x y z qx qy qz qw t
用法: python make_example.py --robot ur5e --n 50 --out ur5e_traj.csv
"""
import argparse
import csv
import os
import sys

import numpy as np
import pinocchio as pin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import robots  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", default="ur5e", choices=robots.list_robots())
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--out", default="example_traj.csv")
    ap.add_argument("--dt", type=float, default=0.1)
    a = ap.parse_args()

    rb = robots.load(a.robot)
    m, data = rb.model, rb.data
    q0 = rb.q_home.copy()
    rng = np.random.default_rng(0)
    amp = 0.4 * np.ones(m.nq)
    phase = rng.random(m.nq) * 2 * np.pi

    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x", "y", "z", "qx", "qy", "qz", "qw", "t"])
        for k in range(a.n):
            s = k / max(a.n - 1, 1) * 2 * np.pi
            q = q0 + amp * np.sin(s + phase)
            q = np.clip(q, rb.q_lo, rb.q_hi)
            pin.framesForwardKinematics(m, data, q)
            T = data.oMf[rb.tcp_id]
            quat = pin.Quaternion(T.rotation)
            w.writerow([round(float(x), 6) for x in T.translation] +
                       [round(float(v), 6) for v in (quat.x, quat.y, quat.z, quat.w)] +
                       [round(k * a.dt, 4)])
    print(f"写出 {a.n} 点 -> {a.out}")


if __name__ == "__main__":
    main()
