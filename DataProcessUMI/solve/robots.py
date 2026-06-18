"""机器人注册表 + 模型加载（Pinocchio + Coal）。

每款机械臂记录：URDF 入口、TCP 末端 frame、需锁定的夹爪关节、可选 SRDF。
加载时：
  - buildReducedModel 锁定夹爪关节（雅可比只剩手臂自由度，条件数才有意义）；
  - 构建 COLLISION 几何模型并启用全部碰撞对；
  - 用 SRDF（若有）+ 相邻关系 + 可选采样标定，屏蔽相邻/恒碰撞的假阳性对。
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pinocchio as pin

# tools/data/solve/robots.py  ->  tools/data/resources
_HERE = os.path.dirname(os.path.abspath(__file__))
RESOURCES = os.environ.get(
    "TRAJ_CHECK_RESOURCES",
    os.path.normpath(os.path.join(_HERE, "..", "resources")),
)
_SHARE = os.path.join(RESOURCES, ".ament", "install", "share")


@dataclass
class RobotSpec:
    name: str
    urdf: str                                  # 相对 RESOURCES 的路径
    tcp_frame: str
    locked_joints: Dict[str, float] = field(default_factory=dict)  # 夹爪关节 -> 锁定值
    srdf: Optional[str] = None                 # 相对 RESOURCES，可选

    def urdf_path(self) -> str:
        return os.path.join(RESOURCES, self.urdf)

    def srdf_path(self) -> Optional[str]:
        return os.path.join(RESOURCES, self.srdf) if self.srdf else None


REGISTRY: Dict[str, RobotSpec] = {
    "franka_fr3v2": RobotSpec(
        "franka_fr3v2", "franka_description/fr3v2.urdf", "fr3v2_hand_tcp",
        {"fr3v2_finger_joint1": 0.0, "fr3v2_finger_joint2": 0.0},
        "franka_description/fr3v2.srdf",
    ),
    "ur5e": RobotSpec("ur5e", "universal_robots/ur5e.urdf", "tool0"),
    "ur7e": RobotSpec("ur7e", "universal_robots/ur7e.urdf", "tool0"),
    "flexiv_rizon4": RobotSpec("flexiv_rizon4", "flexiv_description/rizon4.urdf", "flange"),
    "aloha_piper": RobotSpec(
        "aloha_piper",
        "piper_ros/src/piper_description/urdf/piper_description.urdf", "gripper_base",
        {"joint7": 0.0, "joint8": 0.0},
        "piper_ros/src/piper_moveit/piper_with_gripper_moveit/config/piper.srdf",
    ),
    "arx5_x5": RobotSpec("arx5_x5", "arx5-sdk/models/X5.urdf", "eef_link"),
}


def list_robots() -> List[str]:
    return list(REGISTRY)


def _package_dirs(urdf_path: str) -> List[str]:
    # package:// 经 .ament/share（含 ur_description 等软链接）解析；
    # arx5 用相对 ./meshes，需 urdf 自身目录。
    return [_SHARE, RESOURCES, os.path.dirname(urdf_path)]


class Robot:
    """已加载的机械臂：手臂运动学模型 + 碰撞模型（已屏蔽假阳性对）。"""

    def __init__(self, spec: RobotSpec, calibrate_samples: int = 0,
                 calibrate_thresh: float = 0.95, seed: int = 0):
        self.spec = spec
        urdf = spec.urdf_path()
        pkg = _package_dirs(urdf)

        full = pin.buildModelFromUrdf(urdf)
        geom = pin.buildGeomFromUrdf(full, urdf, pin.GeometryType.COLLISION, pkg)

        # 锁定夹爪关节的参考构型：未锁定关节取限位中点，锁定关节取指定值。
        q_ref = _midpoint_config(full)
        lock_ids = []
        for jname, val in spec.locked_joints.items():
            jid = full.getJointId(jname)
            q_ref[full.joints[jid].idx_q] = val
            lock_ids.append(jid)

        if lock_ids:
            self.model, geoms = pin.buildReducedModel(full, [geom], lock_ids, q_ref)
            self.geom = geoms[0]
        else:
            self.model, self.geom = full, geom

        self.data = self.model.createData()
        self.tcp_id = self.model.getFrameId(spec.tcp_frame)
        if self.tcp_id >= self.model.nframes:
            raise ValueError(f"TCP frame '{spec.tcp_frame}' 不存在")

        # 关节名 / 限位（手臂自由度）
        self.joint_names = [self.model.names[i] for i in range(1, len(self.model.names))]
        self.q_lo = self.model.lowerPositionLimit.copy()
        self.q_hi = self.model.upperPositionLimit.copy()
        self.v_lim = self.model.velocityLimit.copy()
        self.q_home = _midpoint_config(self.model)

        # 碰撞对：全部 -> 去掉 SRDF + 相邻 + 恒碰撞
        self.geom.addAllCollisionPairs()
        if spec.srdf_path() and os.path.exists(spec.srdf_path()):
            pin.removeCollisionPairs(self.model, self.geom, spec.srdf_path())
        self._disable_adjacent()
        if calibrate_samples > 0:
            self._disable_always_colliding(calibrate_samples, calibrate_thresh, seed)
        self.gdata = self.geom.createData()

    # ----- 几何/碰撞对清理 -----
    def _disable_adjacent(self):
        m, g = self.model, self.geom
        keep = []
        for cp in g.collisionPairs:
            ja = g.geometryObjects[cp.first].parentJoint
            jb = g.geometryObjects[cp.second].parentJoint
            adjacent = (ja == jb or m.parents[ja] == jb or m.parents[jb] == ja)
            if not adjacent:
                keep.append(cp)
        g.removeAllCollisionPairs()
        for cp in keep:
            g.addCollisionPair(cp)

    def _disable_always_colliding(self, n: int, thresh: float, seed: int):
        rng = np.random.default_rng(seed)
        npair = len(self.geom.collisionPairs)
        if npair == 0:
            return
        hits = np.zeros(npair, dtype=int)
        gdata = self.geom.createData()
        data = self.model.createData()
        for _ in range(n):
            q = self._random_q(rng)
            pin.computeCollisions(self.model, data, self.geom, gdata, q, False)
            for i, r in enumerate(gdata.collisionResults):
                if r.isCollision():
                    hits[i] += 1
        keep = [cp for i, cp in enumerate(self.geom.collisionPairs)
                if hits[i] / n < thresh]
        self.geom.removeAllCollisionPairs()
        for cp in keep:
            self.geom.addCollisionPair(cp)

    # ----- 采样 -----
    def _random_q(self, rng) -> np.ndarray:
        lo = np.where(np.isfinite(self.q_lo), self.q_lo, -np.pi)
        hi = np.where(np.isfinite(self.q_hi), self.q_hi, np.pi)
        return lo + rng.random(self.model.nq) * (hi - lo)


def _midpoint_config(model: pin.Model) -> np.ndarray:
    lo, hi = model.lowerPositionLimit, model.upperPositionLimit
    q = pin.neutral(model)
    for i in range(model.nq):
        if np.isfinite(lo[i]) and np.isfinite(hi[i]):
            q[i] = 0.5 * (lo[i] + hi[i])
    return q


def load(robot_name: str, **kw) -> Robot:
    if robot_name not in REGISTRY:
        raise KeyError(f"未知机械臂 '{robot_name}'，可选：{list_robots()}")
    return Robot(REGISTRY[robot_name], **kw)
