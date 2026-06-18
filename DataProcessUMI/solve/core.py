"""IK 求解器 + 单点指标（两个程序共用的核心）。

对每个 TCP 目标位姿：
  1. 逆运动学（阻尼最小二乘 CLIK，支持热启动 + 随机重启）；
  2. 位姿残差（位置 mm / 姿态 deg）—— IK 误差估计；
  3. 关节限位 + 余量；
  4. 雅可比最小奇异值 / 条件数 / 可操作度 —— 奇异度（质量）；
  5. 自碰撞 + 最近带符号距离（clearance）—— 安全余量；
  6. 相邻点差分得到关节速度，与速度限位比较。
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import List, Optional

import numpy as np
import pinocchio as pin

from robots import Robot


# ----------------------------- IK -----------------------------
@dataclass
class IKResult:
    q: np.ndarray
    converged: bool
    pos_err_mm: float      # 位置残差
    rot_err_deg: float     # 姿态残差
    iters: int


def solve_ik(robot: Robot, target: pin.SE3, q_seed: Optional[np.ndarray] = None,
             pos_tol: float = 1e-4, rot_tol: float = 1e-3,
             max_iters: int = 200, damp: float = 1e-6,
             restarts: int = 0, rng: Optional[np.random.Generator] = None) -> IKResult:
    """阻尼最小二乘逆运动学。pos_tol[m], rot_tol[rad] 为收敛阈值。"""
    m, data, fid = robot.model, robot.data, robot.tcp_id
    seed = robot.q_home if q_seed is None else q_seed

    best: Optional[IKResult] = None
    tries = [seed] + (
        [robot._random_q(rng or np.random.default_rng(i)) for i in range(restarts)]
    )
    for q0 in tries:
        q = q0.copy()
        it = 0
        for it in range(1, max_iters + 1):
            pin.framesForwardKinematics(m, data, q)
            iMd = data.oMf[fid].actInv(target)          # 当前->目标
            err = pin.log(iMd).vector                   # 6D 误差（局部）
            if (np.linalg.norm(err[:3]) < pos_tol and
                    np.linalg.norm(err[3:]) < rot_tol):
                break
            J = pin.computeFrameJacobian(m, data, q, fid)
            Jlog = pin.Jlog6(iMd.inverse())
            J = -Jlog @ J
            v = -J.T @ np.linalg.solve(J @ J.T + damp * np.eye(6), err)
            q = pin.integrate(m, q, v)
            q = np.clip(q, robot.q_lo, robot.q_hi)

        pin.framesForwardKinematics(m, data, q)
        iMd = data.oMf[fid].actInv(target)
        e = pin.log(iMd).vector
        pos = float(np.linalg.norm(e[:3]) * 1000.0)
        rot = float(np.degrees(np.linalg.norm(e[3:])))
        conv = pos < pos_tol * 1000.0 + 1e-9 and np.radians(rot) < rot_tol + 1e-9
        res = IKResult(q, conv, pos, rot, it)
        if best is None or (conv and not best.converged) or \
                (conv == best.converged and pos + rot < best.pos_err_mm + best.rot_err_deg):
            best = res
        if conv:
            break
    return best


# --------------------------- 指标 ---------------------------
def jacobian_metrics(robot: Robot, q: np.ndarray):
    """返回 (sigma_min, cond, manipulability)。"""
    pin.computeJointJacobians(robot.model, robot.data, q)
    J = pin.getFrameJacobian(robot.model, robot.data, robot.tcp_id,
                             pin.ReferenceFrame.LOCAL_WORLD_ALIGNED)
    s = np.linalg.svd(J, compute_uv=False)
    sigma_min = float(s[-1])
    cond = float(s[0] / s[-1]) if s[-1] > 1e-12 else float("inf")
    manip = float(np.sqrt(max(np.linalg.det(J @ J.T), 0.0)))
    return sigma_min, cond, manip


def joint_limit_check(robot: Robot, q: np.ndarray):
    """返回 (within, min_margin_rad)。"""
    lo = np.where(np.isfinite(robot.q_lo), robot.q_lo, -np.inf)
    hi = np.where(np.isfinite(robot.q_hi), robot.q_hi, np.inf)
    margin = np.minimum(q - lo, hi - q)
    finite = np.isfinite(margin)
    mm = float(margin[finite].min()) if finite.any() else float("inf")
    return bool(mm >= 0.0), mm


def self_collision(robot: Robot, q: np.ndarray):
    """返回 (in_collision, clearance_mm)。clearance>0 安全余量；<0 穿透深度。"""
    m = robot.model
    pin.computeCollisions(m, robot.data, robot.geom, robot.gdata, q, False)
    in_col = any(r.isCollision() for r in robot.gdata.collisionResults)
    pin.computeDistances(m, robot.data, robot.geom, robot.gdata, q)
    dmin = min((dr.min_distance for dr in robot.gdata.distanceResults),
               default=float("inf"))
    return bool(in_col), float(dmin * 1000.0)


# --------------------------- 单点结果 ---------------------------
@dataclass
class PointResult:
    index: int
    t: float
    ik_ok: bool
    pos_err_mm: float
    rot_err_deg: float
    in_limits: bool
    limit_margin_rad: float
    sigma_min: float
    cond: float
    manip: float
    self_collision: bool
    clearance_mm: float
    vel_ratio: float          # max |dq/dt| / vlim（无时间列时为 nan）
    executable: bool
    quality: float            # 0~1
    q: List[float]

    def row(self):
        d = asdict(self)
        d.pop("q")
        return d


@dataclass
class Thresholds:
    pos_tol_mm: float = 1.0           # IK 位置收敛
    rot_tol_deg: float = 0.5          # IK 姿态收敛
    sigma_min: float = 0.02           # 低于则判定接近奇异
    clearance_mm: float = 2.0         # 低于则判定碰撞风险（即便未穿透）
    vel_ratio: float = 1.0            # 关节速度/限位上限


def _quality(pr_pos, pr_rot, sigma_min, clearance_mm, limit_margin, vel_ratio,
             th: Thresholds) -> float:
    """各项归一化后取最小值（短板决定质量）。"""
    f_ik = np.clip(1.0 - (pr_pos / th.pos_tol_mm + pr_rot / th.rot_tol_deg) / 2.0, 0, 1)
    f_sing = np.clip(sigma_min / (5 * th.sigma_min), 0, 1)
    f_col = np.clip(clearance_mm / (5 * th.clearance_mm), 0, 1)
    f_lim = np.clip(limit_margin / 0.2, 0, 1)        # 0.2 rad 余量记满分
    f_vel = 1.0 if np.isnan(vel_ratio) else np.clip(1.0 - vel_ratio / th.vel_ratio, 0, 1)
    return float(min(f_ik, f_sing, f_col, f_lim, f_vel))


def evaluate_point(robot: Robot, target: pin.SE3, index: int, t: float,
                   q_seed: Optional[np.ndarray], th: Thresholds,
                   restarts: int, rng) -> PointResult:
    ik = solve_ik(robot, target, q_seed,
                  pos_tol=th.pos_tol_mm / 1000.0, rot_tol=np.radians(th.rot_tol_deg),
                  restarts=restarts, rng=rng)
    sigma_min, cond, manip = jacobian_metrics(robot, ik.q)
    in_lim, margin = joint_limit_check(robot, ik.q)
    in_col, clr = self_collision(robot, ik.q)

    executable = (ik.converged and in_lim and (not in_col)
                  and sigma_min >= th.sigma_min and clr >= th.clearance_mm)
    quality = _quality(ik.pos_err_mm, ik.rot_err_deg, sigma_min, clr,
                       margin, float("nan"), th)
    return PointResult(index, t, ik.converged, ik.pos_err_mm, ik.rot_err_deg,
                       in_lim, margin, sigma_min, cond, manip, in_col, clr,
                       float("nan"), bool(executable), quality, ik.q.tolist())


# --------------------------- 速度后处理 ---------------------------
def add_velocity_checks(robot: Robot, results: List[PointResult],
                        times: Optional[np.ndarray], th: Thresholds):
    """相邻点差分计算关节速度比，刷新 vel_ratio / executable / quality。"""
    if times is None or len(results) < 2:
        return
    m = robot.model
    for i in range(1, len(results)):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            continue
        qa = np.array(results[i - 1].q)
        qb = np.array(results[i].q)
        dq = pin.difference(m, qa, qb)
        ratio = float(np.max(np.abs(dq / dt) / np.where(robot.v_lim > 0, robot.v_lim, np.inf)))
        r = results[i]
        r.vel_ratio = ratio
        if ratio > th.vel_ratio:
            r.executable = False
        r.quality = min(r.quality,
                        float(np.clip(1.0 - ratio / th.vel_ratio, 0, 1)))


def failure_reason(r: PointResult, th: Thresholds) -> str:
    if not r.ik_ok:
        return "ik_unreachable"
    if not r.in_limits:
        return "joint_limit"
    if r.self_collision:
        return "self_collision"
    if r.sigma_min < th.sigma_min:
        return "near_singular"
    if r.clearance_mm < th.clearance_mm:
        return "collision_margin"
    if not np.isnan(r.vel_ratio) and r.vel_ratio > th.vel_ratio:
        return "velocity_limit"
    return "ok"
