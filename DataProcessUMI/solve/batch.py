"""批量 IK 驱动：沿轨迹热启动求解，可多进程连续分块。

返回 List[PointResult]（含每点关节解 q、IK 收敛标志、残差及各项指标），
供两个程序（可执行性判定 / 关节序列导出）共用。
"""
from __future__ import annotations
from typing import List, Optional

import numpy as np
import pinocchio as pin

import robots
from core import Thresholds, evaluate_point, add_velocity_checks, PointResult


def _run_chunk(args):
    """子进程：重建机器人，对一段连续位姿热启动求解。"""
    (robot_name, calib, poses_se3, start_idx, times_chunk, th, restarts, seed) = args
    rb = robots.load(robot_name, calibrate_samples=calib, seed=seed)
    rng = np.random.default_rng(seed)
    out: List[PointResult] = []
    q_seed = None
    for k, T in enumerate(poses_se3):
        idx = int(start_idx + k)
        t = times_chunk[k] if times_chunk is not None else float(idx)
        # 段首冷启动（带随机重启）；其余热启动延续构型连续性。
        r = evaluate_point(rb, T, idx, t, q_seed, th,
                           restarts=(restarts if k == 0 else 0), rng=rng)
        q_seed = np.array(r.q)
        out.append(r)
    add_velocity_checks(rb, out, times_chunk, th)
    return out


def validate(robot_name: str, poses: List[pin.SE3], times: Optional[np.ndarray],
             th: Thresholds, jobs: int = 1, calibrate: int = 0,
             restarts: int = 4, seed: int = 0) -> List[PointResult]:
    n = len(poses)
    if jobs <= 1 or n < 2 * jobs:
        return _run_chunk((robot_name, calibrate, poses, 0, times, th, restarts, seed))

    # 连续分块，块内热启动；块界处冷启动（带重启）保证独立正确性。
    bounds = np.linspace(0, n, jobs + 1, dtype=int)
    tasks = []
    for j in range(jobs):
        a, b = bounds[j], bounds[j + 1]
        if a == b:
            continue
        tc = times[a:b] if times is not None else None
        tasks.append((robot_name, calibrate, poses[a:b], a, tc, th, restarts, seed + j))
    import multiprocessing as mp
    with mp.Pool(min(jobs, len(tasks))) as pool:
        chunks = pool.map(_run_chunk, tasks)
    res = [r for ch in chunks for r in ch]
    res.sort(key=lambda r: r.index)
    return res
