"""逆运动学交叉验证（独立双解一致性复核）+ 可选 pink(QP-IK) 适配器。

仅在 fit_trajectory 末端对最终摆放做复核，不进入内层搜索（避免成本翻倍）。

cross_check 用两条相互独立的求解路径解算同一条轨迹并比较结果：
  - method="clik"（默认，稳健）：主解=沿轨迹热启动 CLIK；副解=逐点冷启动 + 多次
    随机重启的 CLIK。二者初始化盆地与策略不同，一致 => 强证据；不一致（可行判定相反
    或关节差过大）=> 定位到落在不同 IK 分支 / 数值难解 / 临界的点。
  - method="pink"（实验性）：副解改用 pink 的 QP-IK（关节限位作硬约束）。当前环境下
    pink+QP 在部分目标上收敛不稳定，故非默认；需要时显式开启。

solve_ik_pink 与 core.solve_ik 同接口（SE3 目标 + 热启动，返回 q 与残差）。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pinocchio as pin

import pink
from pink.tasks import FrameTask
from pink.limits import ConfigurationLimit

from robots import Robot
from core import solve_ik


@dataclass
class PinkResult:
    q: np.ndarray
    converged: bool
    pos_err_mm: float
    rot_err_deg: float
    iters: int


def _residual(model, data, fid, q, target: pin.SE3):
    pin.framesForwardKinematics(model, data, q)
    err = pin.log(data.oMf[fid].actInv(target)).vector
    return (float(np.linalg.norm(err[:3]) * 1000.0),
            float(np.degrees(np.linalg.norm(err[3:]))))


def _pick_solver() -> str:
    import qpsolvers
    for s in ("quadprog", "osqp", "daqp", "proxqp", "scs"):
        if s in qpsolvers.available_solvers:
            return s
    if qpsolvers.available_solvers:
        return qpsolvers.available_solvers[0]
    raise RuntimeError("未找到任何 QP 求解器后端，请 `pip install osqp`")


_QP = _pick_solver()


def solve_ik_pink(robot: Robot, target: pin.SE3, q_seed: Optional[np.ndarray] = None,
                  pos_tol: float = 1e-4, rot_tol: float = 1e-3,
                  max_iters: int = 200, dt: float = 1.0) -> PinkResult:
    """QP-IK：迭代速度求解 + 积分，关节限位作硬约束。pos_tol[m], rot_tol[rad]。"""
    model, data = robot.model, robot.data
    fid = robot.tcp_id
    q = (robot.q_home if q_seed is None else q_seed).copy()
    q = np.clip(q, robot.q_lo, robot.q_hi)

    config = pink.Configuration(model, data, q)
    task = FrameTask(robot.spec.tcp_frame, position_cost=1.0, orientation_cost=1.0)
    task.set_target(target)
    limits = [ConfigurationLimit(model)]

    it = 0
    for it in range(1, max_iters + 1):
        pos_mm, rot_deg = _residual(model, data, fid, config.q, target)
        if pos_mm < pos_tol * 1000.0 and rot_deg < np.degrees(rot_tol):
            break
        v = pink.solve_ik(config, [task], dt, solver=_QP, limits=limits)
        config.integrate_inplace(v, dt)

    q = np.clip(config.q, robot.q_lo, robot.q_hi)
    pos_mm, rot_deg = _residual(model, data, fid, q, target)
    conv = pos_mm < pos_tol * 1000.0 + 1e-6 and rot_deg < np.degrees(rot_tol) + 1e-6
    return PinkResult(q, conv, pos_mm, rot_deg, it)


def cross_check(robot: Robot, targets: List[pin.SE3],
                pos_tol_mm: float = 1.0, rot_tol_deg: float = 0.5,
                q_match_tol: float = 0.05, method: str = "clik",
                restarts: int = 8, seed: int = 12345) -> dict:
    """两条独立求解路径解算整条轨迹并比较，定位不一致点。method: 'clik' | 'pink'。"""
    m = robot.model
    pos_tol, rot_tol = pos_tol_mm / 1000.0, np.radians(rot_tol_deg)
    rng = np.random.default_rng(seed)

    def solve_b(T):
        # 副解：冷启动 + 多次随机重启（与主解的热启动盆地相互独立）。
        if method == "pink":
            rk = solve_ik_pink(robot, T, None, pos_tol=pos_tol, rot_tol=rot_tol)
            return rk.q, rk.converged, rk.pos_err_mm, rk.rot_err_deg
        r = solve_ik(robot, T, None, pos_tol=pos_tol, rot_tol=rot_tol,
                     restarts=restarts, rng=rng)
        return r.q, r.converged, r.pos_err_mm, r.rot_err_deg

    q_a = None
    rows, disagree, branch_diff = [], [], []
    for i, T in enumerate(targets):
        ra = solve_ik(robot, T, q_a, pos_tol=pos_tol, rot_tol=rot_tol, restarts=0)
        q_a = ra.q
        qb, b_ok, b_pos, b_rot = solve_b(T)
        dq = float(np.max(np.abs(pin.difference(m, ra.q, qb))))
        rows.append({
            "index": i,
            "a_ok": bool(ra.converged), "b_ok": bool(b_ok),
            "a_pos_mm": round(ra.pos_err_mm, 4), "b_pos_mm": round(b_pos, 4),
            "a_rot_deg": round(ra.rot_err_deg, 4), "b_rot_deg": round(b_rot, 4),
            "max_dq_rad": round(dq, 5),
        })
        # 关键：跨独立求解只比较【可行判定】；冗余臂/多解 IK 下关节值本就不同，
        # 故关节差仅作分支多样性参考，不计入分歧。
        if ra.converged != b_ok:
            disagree.append(i)
        elif ra.converged and b_ok and dq > q_match_tol:
            branch_diff.append(i)

    return {
        "method": f"A=warm-CLIK vs B={'pink-QP' if method=='pink' else 'cold-CLIK+restarts'}",
        "compares": "feasibility verdict + per-point residual (NOT joint values)",
        "n_points": len(targets),
        "a_feasible": sum(r["a_ok"] for r in rows),
        "b_feasible": sum(r["b_ok"] for r in rows),
        "verdict_agree": all(r["a_ok"] == r["b_ok"] for r in rows),
        "n_disagree": len(disagree),
        "disagree_indices": disagree,
        "n_branch_diff": len(branch_diff),                  # 两法落到不同 IK 分支（正常）
        "max_dq_rad": round(max((r["max_dq_rad"] for r in rows), default=0.0), 5),
        "note": "max_dq_rad 大表示两法落在不同 IK 解(冗余/多解所致)，非错误；"
                "可行性判定一致即为通过。",
        "rows": rows,
    }
