# 机械臂 URDF 资源（用于 TCP 轨迹可执行性校验）

校验栈：**Pinocchio**（运动学/雅可比/误差）+ **Coal**（自碰撞，带符号距离）。
安装：`pip install pin`（已含 coal）。无需 ROS。

## 机械臂入口文件

| 机械臂 | URDF 入口 | DOF | 来源仓库/分支 |
|---|---|---|---|
| Franka Research 3 (v2) | `franka_description/fr3v2.urdf` | 9 (7臂+2指) | frankaemika/franka_description @ main，由 `robots/fr3v2/fr3v2.urdf.xacro` 生成 |
| UR5e | `universal_robots/ur5e.urdf` | 6 | UniversalRobots/..._ROS2_Description @ jazzy |
| UR7e | `universal_robots/ur7e.urdf` | 6 | 同上（复用 ur5e 网格） |
| Flexiv Rizon4 | `flexiv_description/rizon4.urdf` | 7 | flexivrobotics/flexiv_description @ **humble-v1**（v2 分支已无 Rizon4） |
| ALOHA Piper | `piper_ros/src/piper_description/urdf/piper_description.urdf` | 8 (6臂+2指) | agilexrobotics/piper_ros @ humble（现成 urdf） |
| ARX5 | `arx5-sdk/models/X5.urdf` | 6 | real-stanford/arx5-sdk @ main（已修 base_link 网格路径 bug） |

## 网格（package://）解析

xacro 生成的 URDF 用 `package://<pkg>/...` 引用网格。加载时把
`.ament/install/share` 加入 `package_dirs` 即可解析（内含各包软链接，
其中 `ur_description -> universal_robots`）：

```python
import os, pinocchio as pin
RES = os.path.dirname(__file__)
SHARE = os.path.join(RES, ".ament/install/share")

def load(urdf):
    dirs = [SHARE, RES, os.path.dirname(urdf)]   # arx5 用相对 ./meshes，需 urdf 自身目录
    model = pin.buildModelFromUrdf(urdf)
    geom  = pin.buildGeomFromUrdf(model, urdf, pin.GeometryType.COLLISION, dirs)
    geom.addAllCollisionPairs()
    return model, geom
```

## SRDF（屏蔽相邻连杆的自碰撞假阳性）

`addAllCollisionPairs()` 会把相邻连杆也算进去，neutral 位姿就会"自碰撞"。
实际校验前用 SRDF 去掉这些对：`pin.removeCollisionPairs(model, geom, srdf_path)`。

- Franka: `franka_description/fr3v2.srdf`（已生成，41 条 disable，123→44 对）
- Piper: `piper_ros/.../config/piper.srdf`（现成）
- UR / Flexiv / ARX5: 仓库无 SRDF，可用相邻关系自动生成或手动列 disable 对。

## 自检

`python3 _loadtest.py` 会加载全部 6 款，构建碰撞模型并在 neutral 做一次自碰撞检查。

## 重新生成 xacro（如需其它型号）

```bash
export PYTHONPATH="$PWD/.ament/shim:$PYTHONPATH"   # 提供 ament_index_python shim
export AMENT_PREFIX_PATH="$PWD/.ament/install"
xacro universal_robots/urdf/ur.urdf.xacro ur_type:=ur10e name:=ur10e > universal_robots/ur10e.urdf
```
（UR 可选型号见 `universal_robots/config/`；Flexiv 见 `flexiv_description/config/`。）
