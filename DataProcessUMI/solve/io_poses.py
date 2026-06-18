"""TCP 位姿 CSV 读取（两个程序共用）。

quat 列序: x y z qx qy qz qw ; rpy 列序: x y z roll pitch yaw(弧度)。
有表头则按列名取列，否则按上述固定顺序。可选时间列（列名 t 或 --time-col）。
以 '#' 开头的行视为注释。
"""
from __future__ import annotations
import csv
from typing import List, Optional, Tuple

import numpy as np
import pinocchio as pin


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def read_poses(path: str, rot: str = "quat", time_col: Optional[str] = None
               ) -> Tuple[List[pin.SE3], Optional[np.ndarray]]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    rows = [r for r in rows if r and not r[0].lstrip().startswith("#")]
    if not rows:
        return [], None
    has_header = any(c.strip() and not _isnum(c) for c in rows[0])
    header = [c.strip() for c in rows[0]] if has_header else None
    data = rows[1:] if has_header else rows

    def col(names, default_idx):
        if header:
            for n in names:
                if n in header:
                    return header.index(n)
        return default_idx

    ix, iy, iz = col(["x"], 0), col(["y"], 1), col(["z"], 2)
    if rot == "quat":
        ia, ib, ic, idd = (col(["qx"], 3), col(["qy"], 4),
                           col(["qz"], 5), col(["qw"], 6))
    else:
        ia, ib, ic = col(["roll", "rx"], 3), col(["pitch", "ry"], 4), col(["yaw", "rz"], 5)

    it = None
    if time_col:
        it = header.index(time_col) if header and time_col in header else None
    elif header and "t" in header:
        it = header.index("t")

    poses, times = [], ([] if it is not None else None)
    for r in data:
        v = [float(x) for x in r]
        p = np.array([v[ix], v[iy], v[iz]])
        if rot == "quat":
            q = pin.Quaternion(v[idd], v[ia], v[ib], v[ic])  # (w,x,y,z)
            q.normalize()
            R = q.matrix()
        else:
            R = pin.rpy.rpyToMatrix(v[ia], v[ib], v[ic])
        poses.append(pin.SE3(R, p))
        if it is not None:
            times.append(v[it])
    return poses, (np.array(times) if times is not None else None)
