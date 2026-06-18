#!/usr/bin/env python3
"""Smoke test: load each robot URDF with Pinocchio + Coal, build the collision
model, and run a self-collision check at the neutral configuration."""
import os
import pinocchio as pin

RES = os.path.dirname(os.path.abspath(__file__))
SHARE = os.path.join(RES, ".ament", "install", "share")
PKG_DIRS = [SHARE, RES]  # package:// resolution (incl. ur_description symlink)

ROBOTS = {
    "franka_fr3v2": "franka_description/fr3v2.urdf",
    "ur5e":         "universal_robots/ur5e.urdf",
    "ur7e":         "universal_robots/ur7e.urdf",
    "flexiv_rizon4":"flexiv_description/rizon4.urdf",
    "aloha_piper":  "piper_ros/src/piper_description/urdf/piper_description.urdf",
    "arx5_x5":      "arx5-sdk/models/X5.urdf",
}

for name, rel in ROBOTS.items():
    urdf = os.path.join(RES, rel)
    try:
        model = pin.buildModelFromUrdf(urdf)
        # relative meshes (arx5) resolve against the urdf dir; package:// via PKG_DIRS
        dirs = PKG_DIRS + [os.path.dirname(urdf)]
        geom = pin.buildGeomFromUrdf(model, urdf, pin.GeometryType.COLLISION, dirs)
        geom.addAllCollisionPairs()
        data = model.createData()
        gdata = geom.createData()
        q = pin.neutral(model)
        pin.computeCollisions(model, data, geom, gdata, q, False)
        ncol = sum(1 for r in gdata.collisionResults if r.isCollision())
        print(f"[OK]   {name:14s} dof={model.nq:2d} links={len(model.names)-1:2d} "
              f"geoms={len(geom.geometryObjects):2d} pairs={len(geom.collisionPairs):3d} "
              f"self-collide@neutral={ncol}")
    except Exception as e:
        print(f"[FAIL] {name:14s} {type(e).__name__}: {str(e)[:120]}")
