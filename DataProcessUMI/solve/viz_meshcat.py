"""MeshCat 可视化：回放求解出的关节轨迹（机械臂本体动画），并叠加目标 TCP 路径
与工作空间盒。导出自包含 HTML（含动画），无需浏览器即可生成，本地打开即播放。

复用 robots.py 的归约逻辑（锁定夹爪关节），构建与 batch 求解一致的归约模型，
但加载 VISUAL 网格用于显示，因此 results 里的关节解 q 维度与模型对齐。
"""
from __future__ import annotations
import os
from typing import List

import numpy as np
import pinocchio as pin

import robots


def _build_visual_model(spec):
    """按 robots.py 的方式归约（锁夹爪），但带 VISUAL/COLLISION 几何用于显示。"""
    urdf = spec.urdf_path()
    pkg = robots._package_dirs(urdf)
    full = pin.buildModelFromUrdf(urdf)
    visual = pin.buildGeomFromUrdf(full, urdf, pin.GeometryType.VISUAL, pkg)
    collision = pin.buildGeomFromUrdf(full, urdf, pin.GeometryType.COLLISION, pkg)

    q_ref = robots._midpoint_config(full)
    lock_ids = []
    for jname, val in spec.locked_joints.items():
        jid = full.getJointId(jname)
        q_ref[full.joints[jid].idx_q] = val
        lock_ids.append(jid)
    if lock_ids:
        model, (visual_r, collision_r) = pin.buildReducedModel(
            full, [visual, collision], lock_ids, q_ref)
        return model, collision_r, visual_r
    return full, collision, visual


def _add_path(viewer, positions, name="tcp_path", color=0x00ff00):
    import meshcat.geometry as g
    pts = np.asarray(positions, dtype=np.float32).T          # 3xN
    viewer[name].set_object(g.Line(
        g.PointsGeometry(pts),
        g.LineBasicMaterial(color=color, linewidth=3)))


def _add_box(viewer, lo, hi, name="workspace", color=0x3366ff, opacity=0.12):
    import meshcat.geometry as g
    import meshcat.transformations as tf
    size = (np.asarray(hi) - np.asarray(lo)).tolist()
    center = (0.5 * (np.asarray(hi) + np.asarray(lo)))
    viewer[name].set_object(
        g.Box(size),
        g.MeshLambertMaterial(color=color, opacity=opacity, transparent=True))
    viewer[name].set_transform(tf.translation_matrix(center))


def animate(robot_name: str, results, shifted_poses: List[pin.SE3],
            w_min=None, w_max=None, dt: float = 0.1, html_out: str = "anim.html"):
    """回放关节轨迹并导出 HTML。results: 含 .q 的逐点结果；shifted_poses: 平移后目标。"""
    from pinocchio.visualize import MeshcatVisualizer
    from meshcat.animation import Animation

    spec = robots.REGISTRY[robot_name]
    model, collision, visual = _build_visual_model(spec)

    viz = MeshcatVisualizer(model, collision, visual)
    viz.initViewer(open=False)
    viz.loadViewerModel()
    real = viz.viewer

    # 叠加：目标 TCP 路径（绿）+ 工作空间盒（蓝，半透明）
    _add_path(real, [P.translation for P in shifted_poses])
    if w_min is not None and w_max is not None:
        _add_box(real, w_min, w_max)

    qs = [np.asarray(r.q, dtype=float) for r in results]
    viz.display(qs[0])

    fps = max(1, int(round(1.0 / dt)))
    anim = Animation(default_framerate=fps)
    for i, q in enumerate(qs):
        with anim.at_frame(real, i) as frame:
            viz.viewer = frame
            viz.display(q)
    viz.viewer = real
    real.set_animation(anim, play=True, repetitions=1)

    html = real.static_html()
    with open(html_out, "w") as f:
        f.write(html)
    return html_out


if __name__ == "__main__":
    import argparse
    import csv
    import sys
    from dataclasses import dataclass

    ap = argparse.ArgumentParser(description="回放 joints.csv 的关节轨迹（MeshCat）")
    ap.add_argument("--robot", required=True, choices=robots.list_robots())
    ap.add_argument("--joints", required=True, help="joints.csv（fit/tcp_to_joints 输出）")
    ap.add_argument("--out", default="anim.html")
    ap.add_argument("--dt", type=float, default=0.1)
    args = ap.parse_args()

    rb = robots.load(args.robot)
    jn = rb.joint_names
    rows = list(csv.DictReader(open(args.joints)))

    @dataclass
    class _R:
        q: list
    res = [_R([float(r[j]) for j in jn]) for r in rows]
    # 无目标路径时用各点 FK 位置作叠加
    poses = []
    for r in res:
        pin.framesForwardKinematics(rb.model, rb.data, np.array(r.q))
        poses.append(rb.data.oMf[rb.tcp_id].copy())
    out = animate(args.robot, res, poses, dt=args.dt, html_out=args.out)
    print("动画已导出:", out)
