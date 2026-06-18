#!/usr/bin/env python3
"""判定一段采集到的 TCP（末端）轨迹能否在各机型本体上执行，并定位**可执行中段**。

针对 `tools/data/resources` 中注册的每一个本体（solve/robots.py:REGISTRY），复用
`tools/data/solve` 的求解逻辑（Pinocchio CLIK + Coal 碰撞）：

  1. 读取某 episode 的 TCP 轨迹（actions/observation 的 eef_pose，6D 旋转）；
     默认先套用 transform 管线（tracker -> world EEF），与 replay 消费的帧一致。
  2. 只允许整条轨迹做 **xyz 整体平移**（姿态不变），搜索一个平移 t*，使其落入本体
     工作空间、且**最长一段连续可执行轨迹**尽可能长（轨迹前后常因够不到 / 姿态越界
     而不可执行，只有中部可解——本工具据此定位中段）；
  3. 在 t* 处全分辨率校验，分别用**严格**与**贴近 replay** 两套阈值判定可执行性；
  4. 输出：能否执行的结论、平移后的 TCP 轨迹、可执行中段的起止帧（连续无间断）、
     以及每个本体的 error 等参数。

每个本体一个子目录，另写一份跨本体/双臂汇总 `summary.json`。

用法：
  # 默认：双臂、对所有本体、套 transform、抽稀到 <=200 点
  python solve_executability.py --episode ~/data/data_samples/.../episode_0000

  # 指定本体 / 单臂 / 已变换数据（不再二次 transform）
  python solve_executability.py --episode <ep> --robots flexiv_rizon4 --arm left \
      --no-transform --jobs 8
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from typing import List, Optional, Tuple

import numpy as np
import pinocchio as pin
from scipy.optimize import minimize
from scipy.spatial import cKDTree

_HERE = os.path.dirname(os.path.abspath(__file__))
_SOLVE = os.path.normpath(os.path.join(_HERE, "..", "solve"))
sys.path.insert(0, _SOLVE)        # 复用 solve 的模块（彼此按裸名 import）
sys.path.insert(0, _HERE)

import robots                                                  # noqa: E402
import fit_trajectory as ft                                    # noqa: E402
from core import Thresholds, evaluate_point, failure_reason    # noqa: E402
from batch import validate                                     # noqa: E402
from check_trajectory import write_report                      # noqa: E402
from workspace_bounds import sample_workspace                  # noqa: E402
from read_episode import read_arm_trajectory                   # noqa: E402

# 两套可执行判据（用户要求两套都输出）：
#   strict —— solve 默认阈值（1mm/0.5°，含奇异/碰撞余量），最保守。
#   replay —— 贴近真实机械臂 replay 容差（~5mm/~3°，放宽奇异/碰撞），更能反映
#             「机械臂实际能否执行」，可执行中段通常更长。
STRICT_TH = Thresholds(pos_tol_mm=1.0, rot_tol_deg=0.5, sigma_min=0.02,
                       clearance_mm=2.0, vel_ratio=1.0)
REPLAY_TH = Thresholds(pos_tol_mm=5.0, rot_tol_deg=3.0, sigma_min=0.005,
                       clearance_mm=0.5, vel_ratio=1.5)


# ----------------------------- 几何 / 工作空间 -----------------------------
def workspace_cloud(robot, x_floor, z_floor, n_samples, seed):
    """台面安装约束下的可达点云（FK 采样）。返回 (pts, w_min, w_max, c_star)。"""
    pts, _, _ = sample_workspace(robot, n_samples, seed)
    mask = (pts[:, 0] >= x_floor) & (pts[:, 2] >= z_floor)
    sub = pts[mask] if mask.any() else pts
    return sub, sub.min(axis=0), sub.max(axis=0), sub.mean(axis=0)


def longest_run(mask) -> Tuple[int, int, int]:
    """最长连续 True 段。返回 (length, start_idx, end_idx)；无则 (0,-1,-1)。"""
    best = cur = 0
    rb = re = -1
    cs = -1
    for i, m in enumerate(mask):
        if m:
            if cur == 0:
                cs = i
            cur += 1
            if cur > best:
                best, rb, re = cur, cs, i
        else:
            cur = 0
    return best, rb, re


# ----------------------------- 平移搜索（最大化可执行中段） -----------------------------
def _position_optimal_offset(P, tree, t_lo, t_hi, c_star, seed):
    """仅按位置：使轨迹各点尽量贴近可达点云的平移（廉价 KD-tree，姿态无关）。"""
    rng = np.random.default_rng(seed)
    span = t_hi - t_lo

    def smooth(t):
        d, _ = tree.query(P + t)
        return float(np.sum(np.maximum(0.0, d - 0.02)))

    seeds = [np.clip(c_star - P.mean(axis=0), t_lo, t_hi)]
    seeds += [t_lo + rng.random(3) * span for _ in range(80)]
    best = min(seeds, key=smooth)
    res = minimize(smooth, best, method="Nelder-Mead",
                   bounds=list(zip(t_lo, t_hi)),
                   options={"maxiter": 300, "xatol": 1e-3, "fatol": 1e-6})
    if res.fun < smooth(best):
        best = np.clip(res.x, t_lo, t_hi)
    return best


def _orient_refine_offset(robot, poses, th, t_lo, t_hi, t0, n_sub, seed, maxiter):
    """姿态相关的廉价精修：对有序子样本沿路热启动求解，最小化总不可行度。"""
    rng = np.random.default_rng(seed)
    n = len(poses)
    sidx = np.linspace(0, n - 1, min(n_sub, n)).astype(int)
    sub = [poses[i] for i in sidx]

    def cost(t):
        q_seed = None
        tot = 0.0
        for Pp in sub:
            Q = Pp.copy()
            Q.translation = Pp.translation + t
            r = evaluate_point(robot, Q, 0, 0.0, q_seed, th, restarts=0, rng=rng)
            q_seed = np.array(r.q)
            tot += ft._point_penalty(r, th)
        return tot

    res = minimize(cost, np.clip(t0, t_lo, t_hi), method="Nelder-Mead",
                   bounds=list(zip(t_lo, t_hi)),
                   options={"maxiter": maxiter, "xatol": 1e-3, "fatol": 1e-2})
    return np.clip(res.x, t_lo, t_hi)


def search_offset_max_segment(robot_name, robot, poses, times, th, t_lo, t_hi,
                              cloud, c_star, args):
    """搜索使**最长连续可执行段**最大的整体平移。

    候选平移 = {灵巧形心播种, 位置最优, 姿态精修} ∪ 各自邻域散点；逐个用（抽稀后的）
    全校验评估其最长连续可执行段，取最优。返回 (best_offset, best_run_len)。
    """
    P = np.array([p.translation for p in poses])
    n = len(poses)
    rng = np.random.default_rng(args.seed)
    span = t_hi - t_lo
    tree = cKDTree(cloud)

    centroid = np.clip(c_star - P.mean(axis=0), t_lo, t_hi)
    posopt = _position_optimal_offset(P, tree, t_lo, t_hi, c_star, args.seed)
    cands = [centroid, posopt]
    # 姿态相关（orientation-aware）种子：solve 的罚函数平移搜索。对大工作空间本体
    # （如 flexiv）能直接找到全可执行摆放并早停；小工作空间本体较慢但更充分。
    sub_idx = ft.pick_waypoints(P, args.waypoints)
    sub = [poses[i] for i in sub_idx]
    pen_t, _ = ft.search_offset(robot, sub, th, t_lo, t_hi, centroid,
                                args.scatter, args.seed)
    cands.append(np.clip(pen_t, t_lo, t_hi))
    if args.orient_refine:
        cands.append(_orient_refine_offset(robot, poses, th, t_lo, t_hi, posopt,
                                           args.refine_points, args.seed,
                                           args.refine_iters))
    # 各种子邻域高斯散点
    base_for_scatter = list(cands)
    for base in base_for_scatter:
        for _ in range(args.scatter):
            cands.append(np.clip(base + rng.normal(0.0, 0.04, 3), t_lo, t_hi))

    # 去重
    uniq, seen = [], set()
    for t in cands:
        k = tuple(np.round(t, 3))
        if k not in seen:
            seen.add(k)
            uniq.append(t)

    # 抽稀以加速搜索评估
    sd = max(1, int(np.ceil(n / args.search_points)))
    s_poses = poses[::sd]
    s_times = times[::sd] if times is not None else None

    best_t, best_run, best_tot = None, -1, -1
    for t in uniq:
        res = validate(robot_name, ft.translate_poses(s_poses, t), s_times, th,
                       jobs=args.jobs, restarts=args.restarts, seed=args.seed)
        mask = [r.executable for r in res]
        L, _, _ = longest_run(mask)
        tot = int(np.sum(mask))
        if (L, tot) > (best_run, best_tot):
            best_t, best_run, best_tot = t.copy(), L, tot
        if best_run == len(s_poses):            # 全可执行，早停
            break
    return best_t, best_run


# ----------------------------- 输出 -----------------------------
def write_tcp_trajectory(poses, times, frames, exec_mask, path):
    """平移后的 TCP 轨迹：x y z qx qy qz qw t frame executable。"""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x", "y", "z", "qx", "qy", "qz", "qw", "t",
                    "frame", "executable"])
        for i, P in enumerate(poses):
            q = pin.Quaternion(P.rotation)
            q.normalize()
            t = float(times[i]) if times is not None else float(i)
            w.writerow([round(float(P.translation[0]), 6),
                        round(float(P.translation[1]), 6),
                        round(float(P.translation[2]), 6),
                        round(float(q.x), 6), round(float(q.y), 6),
                        round(float(q.z), 6), round(float(q.w), 6),
                        round(t, 6), int(frames[i]),
                        int(exec_mask[i])])


def segment_block(results, frames, times, th):
    """从一次全校验结果中提取「最长连续可执行中段」+ 误差汇总。"""
    mask = [r.executable for r in results]
    L, rb, re = longest_run(mask)
    n = len(results)
    n_exec = int(np.sum(mask))
    reasons: dict = {}
    for r in results:
        if not r.executable:
            rs = failure_reason(r, th)
            reasons[rs] = reasons.get(rs, 0) + 1
    block = {
        "thresholds": {
            "pos_tol_mm": th.pos_tol_mm, "rot_tol_deg": th.rot_tol_deg,
            "sigma_min": th.sigma_min, "clearance_mm": th.clearance_mm,
            "vel_ratio": th.vel_ratio,
        },
        "n_executable": n_exec,
        "executable_ratio": round(n_exec / n, 4) if n else 0.0,
        "failure_reasons": reasons,
        "segment_len": L,
        "has_segment": bool(L > 0),
        # 可执行中段：连续无间断的一段（原始帧号 + 抽稀点下标 + 时间）
        "executable_frame_start": int(frames[rb]) if L > 0 else None,
        "executable_frame_end": int(frames[re]) if L > 0 else None,
        "executable_index_start": rb if L > 0 else None,
        "executable_index_end": re if L > 0 else None,
        "executable_time_start_s": round(float(times[rb]), 4) if (L > 0 and times is not None) else None,
        "executable_time_end_s": round(float(times[re]), 4) if (L > 0 and times is not None) else None,
        "error": {
            "max_pos_err_mm": round(max((r.pos_err_mm for r in results), default=0.0), 4),
            "max_rot_err_deg": round(max((r.rot_err_deg for r in results), default=0.0), 4),
            "min_clearance_mm": round(min((r.clearance_mm for r in results), default=0.0), 4),
            "min_sigma": round(min((r.sigma_min for r in results), default=0.0), 5),
            # 仅统计中段内的误差（机械臂真正会执行的部分）：
            "segment_max_pos_err_mm": round(max((results[i].pos_err_mm for i in range(rb, re + 1)), default=0.0), 4) if L > 0 else None,
            "segment_max_rot_err_deg": round(max((results[i].rot_err_deg for i in range(rb, re + 1)), default=0.0), 4) if L > 0 else None,
        },
    }
    return block, (rb, re)


def fit_robot(robot_name, poses, times, frames, outdir, args):
    """对单一本体：搜可行平移 + 双阈值全校验 + 定位中段，写出轨迹/关节/报告。"""
    os.makedirs(outdir, exist_ok=True)
    P = np.array([p.translation for p in poses])
    b_min, b_max = P.min(axis=0), P.max(axis=0)

    robot = robots.load(robot_name)
    x_floor = -np.inf if args.free_space else args.x_floor
    z_floor = -np.inf if args.free_space else args.z_floor
    cloud, w_min, w_max, c_star = workspace_cloud(robot, x_floor, z_floor,
                                                  args.samples, args.seed)

    t_lo, t_hi, boxok, overflow = ft.candidate_offset_box(b_min, b_max, w_min, w_max)
    res = {
        "robot": robot_name,
        "n_points": len(poses),
        "constraint": "free_space" if args.free_space else
                      f"mounted(x>={args.x_floor}, z>={args.z_floor})",
        "traj_bbox": {"min": _r(b_min), "max": _r(b_max)},
        "workspace_bbox": {"min": _r(w_min), "max": _r(w_max)},
        "candidate_offset_box": {"min": _r(t_lo), "max": _r(t_hi)},
    }
    if not boxok:
        axes = "xyz"
        bad = {axes[i]: round(float(overflow[i]), 4) for i in range(3) if overflow[i] > 0}
        res.update({"executable": False, "feasible": False,
                    "stage": "A_geometric_prune",
                    "reason": "trajectory_larger_than_workspace", "overflow_m": bad})
        _dump(outdir, res)
        return res

    # 搜索使可执行中段最长的平移（用 replay 阈值，给出可达性上界更长的中段）
    best_t, best_run = search_offset_max_segment(
        robot_name, robot, poses, times, REPLAY_TH, t_lo, t_hi, cloud, c_star, args)
    res["seed_offset"] = _r(np.clip(c_star - P.mean(axis=0), t_lo, t_hi))
    res["found_offset"] = _r5(best_t)
    res["offset_note"] = ("平移后的 TCP 轨迹 = transform 轨迹 + 此常量 xyz 偏移；"
                          "姿态不变。见 tcp_shifted.csv。")

    # t* 处全分辨率双阈值校验
    shifted = ft.translate_poses(poses, best_t)
    res_strict = validate(robot_name, shifted, times, STRICT_TH, jobs=args.jobs,
                          restarts=args.restarts, seed=args.seed)
    res_replay = validate(robot_name, shifted, times, REPLAY_TH, jobs=args.jobs,
                          restarts=args.restarts, seed=args.seed)
    blk_strict, _ = segment_block(res_strict, frames, times, STRICT_TH)
    blk_replay, (rb, re) = segment_block(res_replay, frames, times, REPLAY_TH)

    # 结论：replay 判据下存在长度 >= min_segment 的连续可执行中段 -> 可执行
    feasible = blk_replay["segment_len"] >= args.min_segment

    # 输出文件：报告（双阈值）、关节（取 replay 解）、平移后 TCP（标注中段）
    report_strict = os.path.join(outdir, "report.strict.csv")
    report_replay = os.path.join(outdir, "report.replay.csv")
    write_report(res_strict, STRICT_TH, report_strict,
                 os.path.join(outdir, "report.strict.summary.json"))
    write_report(res_replay, REPLAY_TH, report_replay,
                 os.path.join(outdir, "report.replay.summary.json"))
    joints_csv = os.path.join(outdir, "joints.csv")
    ft.write_joints(res_replay, robot.joint_names, joints_csv, deg=args.deg)
    exec_mask = [r.executable for r in res_replay]
    tcp_csv = os.path.join(outdir, "tcp_shifted.csv")
    write_tcp_trajectory(shifted, times, frames, exec_mask, tcp_csv)

    res.update({
        "executable": bool(feasible),
        "feasible": bool(feasible),
        "stage": "D_full_validate",
        "min_segment": args.min_segment,
        "strict": blk_strict,
        "replay": blk_replay,
        "outputs": {"tcp_shifted": tcp_csv, "joints": joints_csv,
                    "report_strict": report_strict, "report_replay": report_replay},
    })
    _dump(outdir, res)
    return res


def _r(v):
    return [round(float(x), 4) for x in v]


def _r5(v):
    return [round(float(x), 5) for x in v]


def _dump(outdir, res):
    with open(os.path.join(outdir, "placement.json"), "w") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="判定采集 TCP 轨迹能否在各本体上（整体平移后）执行，并定位可执行中段")
    ap.add_argument("--episode", required=True, help="episode 目录（含 actions.eef_pose）")
    ap.add_argument("--robots", nargs="*", default=None,
                    help="本体子集（默认全部注册本体）")
    ap.add_argument("--arm", default="both", choices=["left", "right", "both"],
                    help="多臂数据解算到单臂本体：默认 both（左右都解）")
    ap.add_argument("--source", default="action", choices=["action", "state"],
                    help="action=动作目标 eef_pose；state=观测实际 eef_pose")
    ap.add_argument("--transform", dest="transform", action="store_true", default=True,
                    help="读取时套用 transform 管线（tracker->world EEF），默认开。"
                         "原始 data_samples 应保持开启；已变换数据请加 --no-transform。")
    ap.add_argument("--no-transform", dest="transform", action="store_false")
    ap.add_argument("--transform-config", default=None, help="transform 配置 JSON")
    ap.add_argument("--outdir", default="out_exec")
    ap.add_argument("--max-points", type=int, default=200,
                    help="全校验抽稀到至多 N 点（0=不抽稀）")
    ap.add_argument("--stride", type=int, default=1, help="抽稀步长")
    ap.add_argument("--min-segment", type=int, default=5,
                    help="判定可执行所需的最短连续中段帧数（抽稀点计）")
    # 搜索
    ap.add_argument("--search-points", type=int, default=100,
                    help="搜索阶段每个候选平移的全校验抽稀点数")
    ap.add_argument("--scatter", type=int, default=4, help="每个种子的邻域散点数")
    ap.add_argument("--waypoints", type=int, default=12,
                    help="姿态相关平移搜索用的代表性路点数")
    ap.add_argument("--orient-refine", action="store_true", default=True,
                    help="姿态相关的廉价平移精修（默认开；小工作空间本体更稳）")
    ap.add_argument("--no-orient-refine", dest="orient_refine", action="store_false")
    ap.add_argument("--refine-points", type=int, default=16)
    ap.add_argument("--refine-iters", type=int, default=60)
    # 摆放约束 / 工作空间
    ap.add_argument("--x-floor", type=float, default=0.0)
    ap.add_argument("--z-floor", type=float, default=0.0)
    ap.add_argument("--free-space", action="store_true")
    ap.add_argument("--samples", type=int, default=80000, help="工作空间采样点数")
    ap.add_argument("--restarts", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--deg", action="store_true", help="关节角以角度输出")
    args = ap.parse_args(argv)

    robot_list = args.robots or robots.list_robots()
    for rn in robot_list:
        if rn not in robots.list_robots():
            print(f"未知本体 '{rn}'，可选：{robots.list_robots()}", file=sys.stderr)
            return 2
    arms = ["left", "right"] if args.arm == "both" else [args.arm]

    ep = os.path.abspath(os.path.expanduser(args.episode))
    os.makedirs(args.outdir, exist_ok=True)
    max_pts = None if args.max_points <= 0 else args.max_points

    overall = {"episode": ep, "source": args.source, "transform": bool(args.transform),
               "results": {}}
    any_exec = False
    for arm in arms:
        poses, times, frames = read_arm_trajectory(
            ep, arm=arm, source=args.source, stride=args.stride,
            max_points=max_pts, transform=args.transform,
            transform_config=args.transform_config)
        dur = times[-1] - times[0] if len(times) else 0.0
        print(f"\n手臂 {arm}: 读到 {len(poses)} 点  (时长 {dur:.1f}s, 源={args.source}, "
              f"transform={'on' if args.transform else 'off'})")
        overall["results"][arm] = {"n_points": len(poses), "robots": {}}
        for rn in robot_list:
            outdir = os.path.join(args.outdir, arm, rn)
            t0 = time.perf_counter()
            res = fit_robot(rn, poses, times, frames, outdir, args)
            dt = time.perf_counter() - t0
            overall["results"][arm]["robots"][rn] = _brief(res)
            _print_one(arm, rn, res, dt)
            if res.get("executable"):
                any_exec = True

    with open(os.path.join(args.outdir, "summary.json"), "w") as f:
        json.dump(overall, f, indent=2, ensure_ascii=False)
    print(f"\n汇总: {os.path.join(args.outdir, 'summary.json')}")
    return 0 if any_exec else 1


def _brief(res):
    b = {"executable": res.get("executable"), "stage": res.get("stage")}
    for k in ("reason", "overflow_m", "found_offset", "strict", "replay", "outputs"):
        if k in res:
            b[k] = res[k]
    return b


def _print_one(arm, rn, res, dt):
    tag = "可执行 ✓" if res.get("executable") else "不可执行 ✗"
    if res.get("stage") == "A_geometric_prune":
        print(f"  [{arm}/{rn}] {tag}  (A 几何裁剪: 轨迹超工作空间 "
              f"{res.get('overflow_m')} m)  {dt:.1f}s")
        return
    sr, rp = res.get("strict", {}), res.get("replay", {})
    print(f"  [{arm}/{rn}] {tag}  平移t*={res.get('found_offset')}  {dt:.1f}s")
    for name, blk in (("strict", sr), ("replay", rp)):
        if not blk:
            continue
        e = blk.get("error", {})
        seg = (f"中段 frame[{blk['executable_frame_start']}..{blk['executable_frame_end']}]"
               f" (连续 {blk['segment_len']} 点)" if blk.get("has_segment") else "无可执行中段")
        print(f"      [{name:6s}] 可执行 {blk['n_executable']}/{res['n_points']}"
              f" ({blk['executable_ratio']*100:.0f}%)  {seg}")
        if blk.get("has_segment"):
            print(f"               中段误差≤{e.get('segment_max_pos_err_mm')}mm/"
                  f"{e.get('segment_max_rot_err_deg')}°  "
                  f"全程间隙≥{e.get('min_clearance_mm')}mm  σmin={e.get('min_sigma')}")
        elif blk.get("failure_reasons"):
            print(f"               失败原因 {blk['failure_reasons']}")


if __name__ == "__main__":
    sys.exit(main())
