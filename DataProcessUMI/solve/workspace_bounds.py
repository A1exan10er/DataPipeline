"""估计各机械臂 TCP 可达工作空间的 xyz 包围盒，并为每款机器人生成 config。

方法：在关节限位内做正运动学（FK）蒙特卡洛采样 —— 对每个随机关节构型求 TCP
在基座坐标系下的位置，统计 xyz 的 min/max。FK 采样直接给出可达点云，比对边界
逐点 IK 更稳健、更快；config 中的上下限即「离开此范围则该位姿无法做出」的判据。

为收紧噪声，min/max 取采样点云的一个分位数（pct~0.1% / 99.9%），避免极少数
临界构型把包围盒撑得过大；同时保留 raw min/max 供参考。

用法：
    python workspace_bounds.py                 # 全部机器人，默认 30 万采样
    python workspace_bounds.py --robots ur5e arx5_x5 --samples 500000
"""
from __future__ import annotations
import argparse
import json
import os
from datetime import date

import numpy as np
import pinocchio as pin

from robots import load, list_robots

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(_HERE, "workspace_configs")


def sample_workspace(robot, n: int, seed: int, batch: int = 20000):
    """返回 (pts[n,3], q_lo, q_hi)：在关节限位内 FK 采样得到的 TCP 位置点云。"""
    m, data, fid = robot.model, robot.data, robot.tcp_id
    rng = np.random.default_rng(seed)
    lo = np.where(np.isfinite(robot.q_lo), robot.q_lo, -np.pi)
    hi = np.where(np.isfinite(robot.q_hi), robot.q_hi, np.pi)

    pts = np.empty((n, 3), dtype=float)
    done = 0
    while done < n:
        k = min(batch, n - done)
        qs = lo + rng.random((k, m.nq)) * (hi - lo)
        for i in range(k):
            pin.framesForwardKinematics(m, data, qs[i])
            pts[done + i] = data.oMf[fid].translation
        done += k
    return pts, lo, hi


def bounds_from_points(pts: np.ndarray, pct: float):
    """返回 raw min/max 与分位数 min/max（收紧极值噪声）。"""
    raw_min = pts.min(axis=0)
    raw_max = pts.max(axis=0)
    q_min = np.percentile(pts, pct, axis=0)
    q_max = np.percentile(pts, 100.0 - pct, axis=0)
    return raw_min, raw_max, q_min, q_max


def mounted_bounds(pts: np.ndarray, x_floor: float, z_floor: float):
    """台面安装约束下的包围盒：仅保留 x>=x_floor 且 z>=z_floor 的可达点。

    返回 (m_min, m_max, kept_ratio)。物理含义：基座固定朝 +x 装在台面上时，
    末端够不到身后（x 不会显著 <0），也不会穿到台面以下（z 不会显著 <0）。
    """
    mask = (pts[:, 0] >= x_floor) & (pts[:, 2] >= z_floor)
    sub = pts[mask]
    if len(sub) == 0:
        return None, None, 0.0
    return sub.min(axis=0), sub.max(axis=0), len(sub) / len(pts)


def build_config(name, spec, robot, raw_min, raw_max, q_min, q_max,
                 n, pct, seed, m_min=None, m_max=None, x_floor=0.0,
                 z_floor=0.0, kept=None):
    """单机器人 config：xyz 上下限 + 出界判据说明。"""
    rnd = lambda a: [round(float(v), 4) for v in a]
    cfg = {
        "robot": name,
        "urdf": spec.urdf,
        "tcp_frame": spec.tcp_frame,
        "frame": "robot_base (URDF root / universe)",
        "units": "meters",
        "method": {
            "type": "forward_kinematics_monte_carlo",
            "samples": n,
            "seed": seed,
            "percentile": pct,
            "note": "workspace 取采样点云的绝对极值（外包络）：保证『目标越出此盒 -> 一定不可达』。"
                    "workspace_inner 为 pct/(100-pct) 分位数收紧的内盒，可作『落在盒内多半可达』的参考。"
                    "注意：AABB 是必要非充分条件——盒内角点（如 xmax,ymax 同时取到）未必可达。",
        },
        "joint_limits_used": {
            jn: [round(float(robot.q_lo[i]), 4), round(float(robot.q_hi[i]), 4)]
            for i, jn in enumerate(robot.joint_names)
        },
        # 主判据：TCP 目标 xyz 任一分量越出外包络 -> 必不可达，机器人做不出该动作。
        "workspace": {
            "x": {"min": round(float(raw_min[0]), 4), "max": round(float(raw_max[0]), 4)},
            "y": {"min": round(float(raw_min[1]), 4), "max": round(float(raw_max[1]), 4)},
            "z": {"min": round(float(raw_min[2]), 4), "max": round(float(raw_max[2]), 4)},
        },
        "workspace_inner": {
            "x": {"min": round(float(q_min[0]), 4), "max": round(float(q_max[0]), 4)},
            "y": {"min": round(float(q_min[1]), 4), "max": round(float(q_max[1]), 4)},
            "z": {"min": round(float(q_min[2]), 4), "max": round(float(q_max[2]), 4)},
        },
        "out_of_workspace_rule": (
            "若目标 TCP 位置的 x/y/z 任一分量 < workspace.<axis>.min 或 "
            "> workspace.<axis>.max，则判定越界：该位姿超出可达工作空间，机器人无法做出对应动作。"
        ),
        "generated": str(date.today()),
    }
    if m_min is not None:
        # 台面安装约束下的实际可用工作空间（基座朝 +x、装在 z=z_floor 台面上）。
        cfg["mounting_assumption"] = {
            "base_frame": "URDF root，原点在安装法兰/台面处",
            "forward_axis": "+x（任务在身前，末端够不到身后）",
            "x_floor": x_floor,
            "z_floor": z_floor,
            "note": "约束：x>=x_floor（不向后）、z>=z_floor（不穿台面）。"
                    "x_max/y/z_max 仍由几何外包络给出，不受影响。",
            "reachable_fraction": round(float(kept), 4) if kept is not None else None,
        }
        cfg["workspace_mounted"] = {
            "x": {"min": round(float(m_min[0]), 4), "max": round(float(m_max[0]), 4)},
            "y": {"min": round(float(m_min[1]), 4), "max": round(float(m_max[1]), 4)},
            "z": {"min": round(float(m_min[2]), 4), "max": round(float(m_max[2]), 4)},
        }
        cfg["mounted_out_of_workspace_rule"] = (
            "固定安装场景下，应以 workspace_mounted 为准：x/y/z 任一越出其 [min,max] "
            "即判定该位姿不可做（含够不到身后 / 穿台面 的物理约束）。"
        )
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robots", nargs="*", default=None,
                    help="机器人名（默认全部）")
    ap.add_argument("--samples", type=int, default=300000)
    ap.add_argument("--percentile", type=float, default=0.1,
                    help="分位数收紧比例（%），默认 0.1 即 [0.1%, 99.9%]")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--x-floor", type=float, default=0.0,
                    help="台面安装：x 下限（不向后），默认 0")
    ap.add_argument("--z-floor", type=float, default=0.0,
                    help="台面安装：z 下限（不穿台面），默认 0")
    args = ap.parse_args()

    names = args.robots or list_robots()
    os.makedirs(CONFIG_DIR, exist_ok=True)

    hdr = f"{'robot':16s} {'x[min,max]':>20s} {'y[min,max]':>20s} {'z[min,max]':>20s}"
    print("== 几何外包络 ==")
    print(hdr)
    summary = {}
    for name in names:
        robot = load(name)
        spec = robot.spec
        pts, _, _ = sample_workspace(robot, args.samples, args.seed)
        raw_min, raw_max, q_min, q_max = bounds_from_points(pts, args.percentile)
        m_min, m_max, kept = mounted_bounds(pts, args.x_floor, args.z_floor)
        cfg = build_config(name, spec, robot, raw_min, raw_max, q_min, q_max,
                           args.samples, args.percentile, args.seed,
                           m_min, m_max, args.x_floor, args.z_floor, kept)
        path = os.path.join(CONFIG_DIR, f"{name}.workspace.json")
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        summary[name] = {"workspace": cfg["workspace"],
                         "workspace_mounted": cfg["workspace_mounted"]}
        fmt = lambda lo, hi: f"[{lo:+.3f},{hi:+.3f}]"
        print(f"{name:16s} "
              f"{fmt(raw_min[0], raw_max[0]):>20s} "
              f"{fmt(raw_min[1], raw_max[1]):>20s} "
              f"{fmt(raw_min[2], raw_max[2]):>20s}")

    print(f"\n== 台面安装可用空间 (x>={args.x_floor}, z>={args.z_floor}) ==")
    print(hdr)
    for name in names:
        w = summary[name]["workspace_mounted"]
        fmt = lambda d: f"[{d['min']:+.3f},{d['max']:+.3f}]"
        print(f"{name:16s} {fmt(w['x']):>20s} {fmt(w['y']):>20s} {fmt(w['z']):>20s}")

    with open(os.path.join(CONFIG_DIR, "summary.workspace.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nconfigs -> {CONFIG_DIR}")


if __name__ == "__main__":
    main()
