# DataProcessUMI — UMI 数据处理、IK 与可执行性管线

本仓库把原始 UMI / 机器人 episode 处理成可用于训练、回放和机器人 IK 校验的数据。主流程是：

```
assessment → preprocess → transform [→ executability]
```

默认前三步完成数据质量门禁、轨迹清洗和 world-base EEF 坐标变换；加
`--run-executability` 后会继续调用 IK / 碰撞检查，输出各机器人本体上的关节轨迹与可执行中段。

## 目录结构

```
DataProcessUMI/
├── pipeline/                 # 一键编排入口
│   └── run_pipeline.py       # assessment → preprocess → transform [→ executability]
├── assessment/               # 阶段 1：有效性评估与门禁
├── preprocess/               # 阶段 2：轨迹突变检测、插值、裁剪、拒绝
├── transform/                # 阶段 3：world-base EEF 坐标变换与腕部视频翻转
├── solve/                    # 通用 IK / 可执行性核心：TCP CSV ↔ 关节轨迹
├── executability/            # 阶段 4：episode 级 IK / 可执行性求解
├── resources/                # URDF/SRDF/mesh 资源，供 solve/executability 加载
├── USAGE.md                  # 使用手册
└── requirements.txt
```

## 安装依赖

```bash
pip install -r requirements.txt
sudo apt install ffmpeg
```

`requirements.txt` 包含基础处理依赖和 IK 依赖 `pin`（Pinocchio + Coal）。可选功能：
`solve/fit_trajectory.py --viz` 需要 `meshcat`；`--cross-method pink` 需要 `pin-pink` 和 QP 求解器。

## 一键处理

```bash
# QA/清洗/坐标变换
python3 pipeline/run_pipeline.py /path/to/class_or_dataset -o pipeline_out

# 同时运行 IK / 可执行性求解
python3 pipeline/run_pipeline.py /path/to/class_or_dataset -o pipeline_out \
    --run-executability --ik-robots flexiv_rizon4 ur5e --ik-arm both --ik-jobs 8
```

输出：

```
pipeline_out/
├── data/<class>_w_world_base/episode_XXXX/
├── report/<class>/episode_XXXX.json
├── report/<class>/episode_XXXX/executability/   # 开启 --run-executability 时
└── report/pipeline_report.json
```

每条报告记录 `status`、失败阶段、assessment 门禁、轨迹分类、平滑/裁剪、transform 记录，
以及可选的 executability summary。IK 阶段用于报告机器人可执行性；是否可执行不改变前三阶段
产出的 `passed` 数据状态。

## 独立 IK 工具

`solve/` 可直接处理 TCP 位姿 CSV：

```bash
# 判定一段 TCP 轨迹是否可执行
python3 solve/check_trajectory.py --robot ur5e --input traj.csv --time-col t --out report.csv

# 解算关节轨迹
python3 solve/tcp_to_joints.py --robot ur5e --input traj.csv --time-col t --out joints.csv

# 为越界轨迹搜索整体 xyz 平移，输出 placement/report/joints
python3 solve/fit_trajectory.py --robot ur5e --input traj.csv --time-col t --outdir out_fit
```

`executability/` 读取 episode 的 `actions.eef_pose` 或 `observation.state.eef_pose`，默认可对左右臂和
所有注册机器人求解：

```bash
python3 executability/solve_executability.py --episode /path/to/episode_0001 \
    --robots flexiv_rizon4 --arm left --no-transform --jobs 8
```

对原始未 transform 的 episode 使用默认 transform；对主 pipeline 输出的
`*_w_world_base/episode_XXXX` 使用 `--no-transform`，避免二次坐标变换。

## 支持机器人

机器人注册表在 `solve/robots.py`：

| `--robot` | TCP frame | 资源 |
| --- | --- | --- |
| `franka_fr3v2` | `fr3v2_hand_tcp` | `resources/franka_description/` |
| `ur5e` / `ur7e` | `tool0` | `resources/universal_robots/` |
| `flexiv_rizon4` | `flange` | `resources/flexiv_description/` |
| `aloha_piper` | `gripper_base` | `resources/piper_ros/` |
| `arx5_x5` | `eef_link` | `resources/arx5-sdk/` |

`resources/.ament/install/share` 提供 `package://` 网格解析软链接；资源说明见
`resources/README.md`。

## 使用手册

完整参数和报告字段见：

- `USAGE.md`：端到端处理与 IK 接入手册。
- `pipeline/README.md`：一键管线参数。
- `solve/README.md`、`solve/USAGE.md`：TCP CSV 级 IK / 可执行性工具。
- `executability/README.md`：episode 级可执行性求解。
