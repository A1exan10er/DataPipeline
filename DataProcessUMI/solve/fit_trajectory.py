#!/usr/bin/env python3
"""程序三：给定机器人，为一段 TCP 轨迹寻找一个可行的整体平移（只平移 xyz，
不改姿态），使整条轨迹可执行；并算误差、出关节轨迹、可视化。

只允许整体平移，所以未知量只有 t∈R³。分阶段 由廉价到昂贵 求解：

  Stage A  几何裁剪：轨迹位置包围盒 B 必须整体落进机器人(台面安装)工作空间盒 W，
           => t 落在闭式候选盒 [Wmin-Bmin, Wmax-Bmax]。某轴为空 => 轨迹比工作空间宽，
           直接判不可行（给出溢出量）。
  Stage B  灵巧中心播种：t0 = (可达点云形心 c*) - (轨迹形心)，多数情况下一步即近可行。
  Stage C  3 自由度搜索：在候选盒内对 K 个最难路点(包围盒极值)算标量罚函数
           f(t)（IK 残差/越限/碰撞/奇异），粗撒点 + Nelder-Mead 精修；f=0 即全可行，
           找到一个就早停（只需一个可行摆放）。
  Stage D  全分辨率校验：对最终 t* 用 batch.validate 跑整条轨迹（含速度/连续性等全部检查）。

求解器：Pinocchio CLIK（core.solve_ik，热启动保证构型连续）。误差/关节轨迹复用现有
report/joints 写出。可选 --cross-check 用独立双解(或 pink)复核最终轨迹。

用法：
  python fit_trajectory.py --robot flexiv_rizon4 --input traj.csv --rot quat \
      --outdir out_fit --cross-check --viz
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys

import numpy as np
import pinocchio as pin
from scipy.optimize import minimize

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robots                                              # noqa: E402
from core import Thresholds, evaluate_point, failure_reason  # noqa: E402
from io_poses import read_poses                            # noqa: E402
from batch import validate                                 # noqa: E402
from check_trajectory import write_report                  # noqa: E402
from workspace_bounds import sample_workspace              # noqa: E402


# ----------------------------- 几何 -----------------------------
def translate_poses(poses, t):
    """整体平移：仅平移 translation，姿态不变。"""
    out = []
    for P in poses:
        Q = P.copy()
        Q.translation = P.translation + t
        out.append(Q)
    return out


def workspace_and_seed(robot, x_floor, z_floor, n_samples, seed):
    """返回 (W_min, W_max, c_star)：台面安装可达点云的包围盒与形心(灵巧中心近似)。"""
    pts, _, _ = sample_workspace(robot, n_samples, seed)
    mask = (pts[:, 0] >= x_floor) & (pts[:, 2] >= z_floor)
    sub = pts[mask] if mask.any() else pts
    return sub.min(axis=0), sub.max(axis=0), sub.mean(axis=0)


def candidate_offset_box(b_min, b_max, w_min, w_max):
    """t 须使 B+t ⊆ W：逐轴 t∈[w_min-b_min, w_max-b_max]。返回 (t_lo,t_hi,ok,overflow)。"""
    t_lo = w_min - b_min
    t_hi = w_max - b_max
    overflow = np.maximum(0.0, (b_max - b_min) - (w_max - w_min))  # 轨迹比工作空间宽多少
    ok = bool(np.all(t_lo <= t_hi + 1e-9))
    return t_lo, t_hi, ok, overflow


def pick_waypoints(positions, k):
    """选最难的代表性路点：每轴 min/max 极值点 + 首尾 + 等距补足到 k。"""
    n = len(positions)
    idx = {0, n - 1}
    for ax in range(3):
        idx.add(int(np.argmin(positions[:, ax])))
        idx.add(int(np.argmax(positions[:, ax])))
    if k > len(idx):
        for j in np.linspace(0, n - 1, k - len(idx) + 2, dtype=int):
            idx.add(int(j))
    return sorted(idx)


# ----------------------------- 罚函数 -----------------------------
def _point_penalty(r, th: Thresholds):
    """单点不可行度；r 可行时为 0。"""
    pen = max(0.0, r.pos_err_mm - th.pos_tol_mm)
    pen += 10.0 * max(0.0, r.rot_err_deg - th.rot_tol_deg)
    pen += 1000.0 * max(0.0, -r.limit_margin_rad)          # 越限，重罚
    pen += max(0.0, th.clearance_mm - r.clearance_mm)
    pen += 1000.0 * max(0.0, th.sigma_min - r.sigma_min)   # 接近奇异
    pen += 100.0 if r.self_collision else 0.0
    if not r.ik_ok:
        pen += 100.0 + r.pos_err_mm                        # 够不到
    return pen


def offset_cost(robot, sub_poses, t, th, rng):
    """把子集路点整体平移 t 后沿路热启动求解，返回总不可行度（0 ⇔ 全可行）。"""
    q_seed = None
    total = 0.0
    for P in sub_poses:
        Q = P.copy()
        Q.translation = P.translation + t
        r = evaluate_point(robot, Q, 0, 0.0, q_seed, th, restarts=0, rng=rng)
        q_seed = np.array(r.q)
        total += _point_penalty(r, th)
    return total


def search_offset(robot, sub_poses, th, t_lo, t_hi, t0, n_scatter, seed):
    """Stage B/C：播种 + 粗撒点 + Nelder-Mead 精修。返回 (best_t, best_cost)。"""
    rng = np.random.default_rng(seed)
    cand = [np.clip(t0, t_lo, t_hi)]
    # 候选盒内低差异散点（含中心）。
    span = t_hi - t_lo
    for _ in range(n_scatter):
        cand.append(t_lo + rng.random(3) * span)
    cand.append(0.5 * (t_lo + t_hi))

    best_t, best_c = None, np.inf
    for t in cand:
        c = offset_cost(robot, sub_poses, t, th, rng)
        if c < best_c:
            best_t, best_c = t.copy(), c
        if best_c == 0.0:
            return best_t, 0.0                              # 早停：已找到可行摆放

    bounds = list(zip(t_lo, t_hi))
    res = minimize(lambda t: offset_cost(robot, sub_poses, t, th, rng),
                   best_t, method="Nelder-Mead", bounds=bounds,
                   options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 400})
    if res.fun < best_c:
        best_t, best_c = np.clip(res.x, t_lo, t_hi), float(res.fun)
    return best_t, best_c


# ----------------------------- 输出 -----------------------------
def write_joints(results, jnames, path, deg=False):
    scale = np.degrees if deg else (lambda x: x)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["index", "t"] + jnames + ["ik_ok", "pos_err_mm", "rot_err_deg"])
        for r in results:
            q = [round(float(x), 6) for x in scale(np.array(r.q))]
            w.writerow([r.index, round(r.t, 6)] + q +
                       [int(r.ik_ok), round(r.pos_err_mm, 5), round(r.rot_err_deg, 5)])


def main(argv=None):
    ap = argparse.ArgumentParser(description="为 TCP 轨迹寻找可行整体平移并求解/可视化")
    ap.add_argument("--robot", required=True, choices=robots.list_robots())
    ap.add_argument("--input", required=True, help="TCP 位姿 CSV")
    ap.add_argument("--rot", default="quat", choices=["quat", "rpy"])
    ap.add_argument("--time-col", default=None)
    ap.add_argument("--outdir", default="out_fit")
    ap.add_argument("--x-floor", type=float, default=0.0, help="台面安装 x 下限")
    ap.add_argument("--z-floor", type=float, default=0.0, help="台面安装 z 下限")
    ap.add_argument("--free-space", action="store_true",
                    help="不套用台面约束，用完整几何外包络搜索")
    ap.add_argument("--samples", type=int, default=200000, help="工作空间采样点数")
    ap.add_argument("--waypoints", type=int, default=16, help="搜索用子集路点数")
    ap.add_argument("--scatter", type=int, default=24, help="候选盒内粗撒点数")
    ap.add_argument("--restarts", type=int, default=4, help="全校验段首 IK 随机重启")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--pos-tol-mm", type=float, default=1.0)
    ap.add_argument("--rot-tol-deg", type=float, default=0.5)
    ap.add_argument("--sigma-min", type=float, default=0.02)
    ap.add_argument("--clearance-mm", type=float, default=2.0)
    ap.add_argument("--vel-ratio", type=float, default=1.0)
    ap.add_argument("--cross-check", action="store_true", help="末端独立双解复核")
    ap.add_argument("--cross-method", default="clik", choices=["clik", "pink"])
    ap.add_argument("--viz", action="store_true", help="导出 MeshCat 动画 HTML")
    ap.add_argument("--deg", action="store_true", help="关节角以角度输出")
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    th = Thresholds(args.pos_tol_mm, args.rot_tol_deg, args.sigma_min,
                    args.clearance_mm, args.vel_ratio)
    poses, times = read_poses(args.input, args.rot, args.time_col)
    if not poses:
        print("没有读到位姿", file=sys.stderr)
        return 2
    P = np.array([p.translation for p in poses])
    b_min, b_max = P.min(axis=0), P.max(axis=0)

    robot = robots.load(args.robot)
    x_floor = -np.inf if args.free_space else args.x_floor
    z_floor = -np.inf if args.free_space else args.z_floor
    w_min, w_max, c_star = workspace_and_seed(robot, x_floor, z_floor,
                                              args.samples, args.seed)

    # Stage A：闭式候选盒
    t_lo, t_hi, boxok, overflow = candidate_offset_box(b_min, b_max, w_min, w_max)
    placement = {
        "robot": args.robot, "input": os.path.abspath(args.input),
        "n_points": len(poses),
        "constraint": "free_space" if args.free_space else
                      f"mounted(x>={args.x_floor}, z>={args.z_floor})",
        "traj_bbox": {"min": [round(float(v), 4) for v in b_min],
                      "max": [round(float(v), 4) for v in b_max]},
        "workspace_bbox": {"min": [round(float(v), 4) for v in w_min],
                           "max": [round(float(v), 4) for v in w_max]},
        "candidate_offset_box": {"min": [round(float(v), 4) for v in t_lo],
                                 "max": [round(float(v), 4) for v in t_hi]},
    }
    if not boxok:
        axes = "xyz"
        bad = {axes[i]: round(float(overflow[i]), 4) for i in range(3) if overflow[i] > 0}
        placement.update({"feasible": False, "stage": "A_geometric_prune",
                          "reason": "轨迹尺寸超出工作空间", "overflow_m": bad})
        _dump(args.outdir, placement)
        print(f"[A] 不可行：轨迹在轴 {list(bad)} 上比工作空间更宽 {bad} m")
        return 1

    # Stage B/C：播种 + 搜索
    sub_idx = pick_waypoints(P, args.waypoints)
    sub_poses = [poses[i] for i in sub_idx]
    t0 = c_star - P.mean(axis=0)
    best_t, best_c = search_offset(robot, sub_poses, th, t_lo, t_hi, t0,
                                   args.scatter, args.seed)
    placement["seed_offset"] = [round(float(v), 4) for v in np.clip(t0, t_lo, t_hi)]
    placement["found_offset"] = [round(float(v), 5) for v in best_t]
    placement["subset_cost"] = round(float(best_c), 5)
    placement["n_waypoints"] = len(sub_idx)

    # Stage D：全分辨率校验
    shifted = translate_poses(poses, best_t)
    results = validate(args.robot, shifted, times, th, jobs=args.jobs,
                       restarts=args.restarts, seed=args.seed)
    report_csv = os.path.join(args.outdir, "report.csv")
    summary_json = os.path.join(args.outdir, "report.summary.json")
    summary = write_report(results, th, report_csv, summary_json)
    joints_csv = os.path.join(args.outdir, "joints.csv")
    write_joints(results, robot.joint_names, joints_csv, deg=args.deg)

    feasible = summary["executable_ratio"] == 1.0
    placement.update({
        "feasible": bool(feasible),
        "stage": "D_full_validate",
        "executable_ratio": summary["executable_ratio"],
        "n_executable": summary["n_executable"],
        "first_failure_index": summary["first_failure_index"],
        "failure_reasons": summary["failure_reasons"],
        "max_pos_err_mm": summary["max_pos_err_mm"],
        "max_rot_err_deg": summary["max_rot_err_deg"],
        "min_clearance_mm": summary["min_clearance_mm"],
        "min_sigma": summary["min_sigma"],
        "worst_quality": summary["worst_quality"],
        "outputs": {"report": report_csv, "joints": joints_csv,
                    "summary": summary_json},
    })

    # 可选交叉验证
    if args.cross_check:
        from ik_pink import cross_check
        cc = cross_check(robot, shifted, args.pos_tol_mm, args.rot_tol_deg,
                         method=args.cross_method)
        cc_path = os.path.join(args.outdir, "cross_check.json")
        with open(cc_path, "w") as f:
            json.dump(cc, f, indent=2, ensure_ascii=False)
        placement["cross_check"] = {
            "method": cc["method"], "verdict_agree": cc["verdict_agree"],
            "n_disagree": cc["n_disagree"], "disagree_indices": cc["disagree_indices"],
            "n_branch_diff": cc["n_branch_diff"], "max_dq_rad": cc["max_dq_rad"],
            "file": cc_path}

    # 可选可视化
    if args.viz:
        try:
            from viz_meshcat import animate
            html = os.path.join(args.outdir, "anim.html")
            animate(args.robot, results, shifted, w_min, w_max, html_out=html)
            placement["outputs"]["viz"] = html
        except Exception as e:                              # 可视化失败不影响主结果
            placement["viz_error"] = str(e)

    _dump(args.outdir, placement)
    _print_summary(placement, summary, args)
    return 0 if feasible else 1


def _dump(outdir, placement):
    with open(os.path.join(outdir, "placement.json"), "w") as f:
        json.dump(placement, f, indent=2, ensure_ascii=False)


def _print_summary(placement, summary, args):
    off = placement["found_offset"]
    print(f"机械臂: {args.robot}  点数: {placement['n_points']}  "
          f"约束: {placement['constraint']}")
    print(f"候选平移盒: min={placement['candidate_offset_box']['min']} "
          f"max={placement['candidate_offset_box']['max']}")
    print(f"找到平移 t* = {off}  (子集罚={placement['subset_cost']})")
    print(f"全校验可执行: {summary['n_executable']}/{summary['n_points']} "
          f"({summary['executable_ratio']*100:.1f}%)  "
          f"-> {'可行 ✓' if placement['feasible'] else '不可行 ✗'}")
    if summary["failure_reasons"]:
        print(f"  失败原因: {summary['failure_reasons']}  首点: {summary['first_failure_index']}")
    print(f"最大IK误差: {summary['max_pos_err_mm']:.3f}mm / {summary['max_rot_err_deg']:.3f}deg  "
          f"最小奇异值: {summary['min_sigma']:.4f}  最小间隙: {summary['min_clearance_mm']:.2f}mm")
    if "cross_check" in placement:
        cc = placement["cross_check"]
        print(f"交叉验证({cc['method']}): 可行判定{'一致 ✓' if cc['verdict_agree'] else '有分歧 ✗'}  "
              f"判定分歧 {cc['n_disagree']} 个 / 不同IK分支 {cc['n_branch_diff']} 个(正常)")
    print(f"输出目录: {args.outdir}")


if __name__ == "__main__":
    sys.exit(main())
