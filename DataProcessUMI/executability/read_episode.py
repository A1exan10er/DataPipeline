"""从 ~/data/data_samples 的采集 episode 读取 TCP（末端）轨迹。

data_samples 中每个 episode 的 EEF 轨迹存为：
  <episode>/actions.eef_pose/data.csv         （动作，下发给末端的目标位姿）
  <episode>/observation.state.eef_pose/data.csv （观测，末端实际位姿）

CSV 列（双臂）：
  timestamp_ms,
  left_x,left_y,left_z, left_r1..left_r6, left_gripper,
  right_x,right_y,right_z, right_r1..right_r6, right_gripper

位置单位为米；姿态用 **6D 旋转表示**（旋转矩阵前两列 a=(r1,r2,r3), b=(r4,r5,r6)，
Zhou et al. 2019），需经 Gram-Schmidt 还原为正交旋转矩阵。

本模块把某一只手臂的一段轨迹转成 solve 所需的 `(List[pin.SE3], times[s])`，
可直接喂给 solve 的求解/平移/校验流程。
"""
from __future__ import annotations
import csv
import os
import sys
from typing import List, Optional, Tuple

import numpy as np
import pinocchio as pin

ARMS = ("left", "right")
SOURCES = {"action": "actions.eef_pose",
           "state": "observation.state.eef_pose"}

# transform/ee_transform.py（tracker -> world EEF）。延迟加载，仅 --transform 时用。
_TRANSFORM_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "transform"))


def _load_transform(config_path: Optional[str] = None):
    """返回 (transform_fn, config)。transform_fn(pos, rot6, side) -> (pos', rot6')。"""
    if _TRANSFORM_DIR not in sys.path:
        sys.path.insert(0, _TRANSFORM_DIR)
    import ee_transform as eet  # noqa: E402
    cfg = eet.load_config(config_path)
    return eet.transform_tracker_pose_to_world_eef_pose, cfg


def sixd_to_matrix(r: np.ndarray) -> np.ndarray:
    """6D 旋转表示 -> 3x3 旋转矩阵（Gram-Schmidt 正交化）。

    r = [a(3), b(3)]，a/b 为旋转矩阵的前两列；返回正交化后的 R=[b1|b2|b3]。
    """
    a, b = r[:3].astype(float), r[3:6].astype(float)
    na = np.linalg.norm(a)
    if na < 1e-9:
        return np.eye(3)
    b1 = a / na
    b2 = b - np.dot(b1, b) * b1
    nb2 = np.linalg.norm(b2)
    if nb2 < 1e-9:                       # a、b 共线，退化：随便补一个正交向量
        tmp = np.array([1.0, 0.0, 0.0]) if abs(b1[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        b2 = tmp - np.dot(b1, tmp) * b1
        nb2 = np.linalg.norm(b2)
    b2 = b2 / nb2
    b3 = np.cross(b1, b2)
    return np.column_stack([b1, b2, b3])


def _episode_csv(episode_dir: str, source: str) -> str:
    if source not in SOURCES:
        raise KeyError(f"未知 source '{source}'，可选：{list(SOURCES)}")
    path = os.path.join(episode_dir, SOURCES[source], "data.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 {source} 轨迹：{path}")
    return path


def read_arm_trajectory(
    episode_dir: str,
    arm: str = "right",
    source: str = "action",
    stride: int = 1,
    max_points: Optional[int] = None,
    transform: bool = False,
    transform_config: Optional[str] = None,
) -> Tuple[List[pin.SE3], np.ndarray, np.ndarray]:
    """读取单臂 TCP 轨迹。

    arm: 'left' | 'right'
    source: 'action'（动作目标）| 'state'（观测实际）
    stride: 抽稀步长；max_points: 自动选步长使点数 <= max_points（优先于 stride）。
    transform: True 时对每帧套用 transform/ee_transform 的 tracker->world EEF 变换
        （等价于先过 transform 管线），适合直接喂未变换的原始 data_samples。
        若 episode 已是 transform 管线输出，请保持 False，避免二次变换。
    transform_config: transform 配置 JSON 路径（默认 transform/ee_trajectory_config.json）。

    返回 (poses[SE3, world 下的 TCP 目标], times[s], frame_indices[原始 CSV 行号])。
    frame_indices 给出每个抽稀点对应的**原始帧号**，用于报告 executable_frame_start/end。
    """
    if arm not in ARMS:
        raise KeyError(f"未知 arm '{arm}'，可选：{ARMS}")
    path = _episode_csv(episode_dir, source)
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    header = [c.strip() for c in rows[0]]
    idx = {name: i for i, name in enumerate(header)}

    def col(name):
        if name not in idx:
            raise KeyError(f"列 '{name}' 不在 {path} 中；表头={header}")
        return idx[name]

    it = col("timestamp_ms")
    ipos = [col(f"{arm}_{c}") for c in ("x", "y", "z")]
    irot = [col(f"{arm}_r{k}") for k in range(1, 7)]

    tf_fn = tf_cfg = None
    if transform:
        tf_fn, tf_cfg = _load_transform(transform_config)

    data = rows[1:]
    n = len(data)
    if max_points is not None and max_points > 0 and n > max_points:
        stride = max(stride, int(np.ceil(n / max_points)))

    poses: List[pin.SE3] = []
    times: List[float] = []
    frames: List[int] = []
    for orig_i in range(0, n, stride):
        v = np.array([float(x) for x in data[orig_i]], dtype=float)
        p = v[ipos]
        rot6 = v[irot]
        if tf_fn is not None:
            p, rot6 = tf_fn(p, rot6, side=arm, config=tf_cfg)
        R = sixd_to_matrix(np.asarray(rot6, dtype=float))
        poses.append(pin.SE3(R, np.asarray(p, dtype=float)))
        times.append(v[it] / 1000.0)            # ms -> s
        frames.append(orig_i)
    return poses, np.array(times), np.array(frames, dtype=int)


def find_episodes(root: str) -> List[str]:
    """递归收集 root 下所有含 actions.eef_pose/data.csv 的 episode 目录。"""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if os.path.basename(dirpath) == SOURCES["action"] and "data.csv" in files:
            out.append(os.path.dirname(dirpath))
    return sorted(out)
