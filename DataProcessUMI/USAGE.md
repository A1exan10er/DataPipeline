# 使用手册

## 1. 安装

```bash
pip install -r requirements.txt
sudo apt install ffmpeg
```

基础数据处理需要 `numpy/scipy/opencv-python/tqdm` 和 `ffmpeg/ffprobe`。IK / 可执行性需要
`pin`（Pinocchio + Coal），机器人 URDF/mesh 在 `resources/`。

首次使用 IK 前确认资源软链接存在：

```bash
ls resources/.ament/install/share
```

应能看到 `ur_description`、`franka_description`、`piper_description`、`flexiv_description` 等入口。

## 2. 一键数据处理

```bash
python3 pipeline/run_pipeline.py /path/to/class_or_dataset -o pipeline_out
```

输入可以是单个 `episode_XXXX`、一个类别目录，或包含多类数据的根目录。输出：

```
pipeline_out/
├── data/<class>_w_world_base/episode_XXXX/
├── report/<class>/episode_XXXX.json
└── report/pipeline_report.json
```

常用调试参数：

```bash
python3 pipeline/run_pipeline.py /path/to/data -o pipeline_out \
    --skip-assessment --keep-intermediate --overwrite
```

## 3. 在管线内运行 IK / 可执行性

```bash
python3 pipeline/run_pipeline.py /path/to/data -o pipeline_out \
    --run-executability \
    --ik-robots flexiv_rizon4 ur5e \
    --ik-arm both \
    --ik-jobs 8
```

管线会先产出已清洗和 transform 的 episode，然后对这些输出 episode 运行
`executability/solve_executability.py --no-transform`。结果写入：

```
report/<class>/episode_XXXX/executability/
├── summary.json
└── <arm>/<robot>/
    ├── placement.json
    ├── tcp_shifted.csv
    ├── joints.csv
    ├── report.strict.csv
    └── report.replay.csv
```

`report/<class>/episode_XXXX.json` 的 `executability` 字段会内嵌 summary，便于下游统一读取。

常用 IK 参数：

| 参数 | 说明 |
| --- | --- |
| `--ik-robots ...` | 机器人子集；不传则跑全部注册机器人。 |
| `--ik-arm left|right|both` | 单臂或双臂，默认 `both`。 |
| `--ik-source action|state` | 使用动作目标或观测状态 eef_pose，默认 `action`。 |
| `--ik-max-points N` | 全校验最多抽稀到 N 点，`0` 不抽稀。 |
| `--ik-min-segment N` | 判定可执行所需的最短连续中段长度。 |
| `--ik-jobs N` | 并行 IK / 碰撞检查进程数。 |
| `--ik-samples N` | 每个机器人的工作空间采样点数。 |
| `--ik-extra-args "..."` | 透传给 `solve_executability.py`，例如 `"--free-space --seed 1"`。 |

## 4. 独立 episode 可执行性求解

对原始未 transform 的 episode，默认会在读取时套用 transform：

```bash
python3 executability/solve_executability.py --episode /path/to/raw/episode_0001 \
    --robots flexiv_rizon4 --arm left --jobs 8
```

对主 pipeline 输出的 `*_w_world_base/episode_XXXX`，必须避免二次 transform：

```bash
python3 executability/solve_executability.py \
    --episode pipeline_out/data/<class>_w_world_base/episode_0001 \
    --robots flexiv_rizon4 --arm left --no-transform --jobs 8
```

退出码：至少一个 `(arm, robot)` 找到可执行中段为 `0`，否则为 `1`。

## 5. 独立 TCP CSV IK 工具

准备 CSV，每行一个 TCP 位姿：

```csv
x,y,z,qx,qy,qz,qw,t
0.722149,-0.189332,0.367597,0.84986,0.474986,0.070151,0.217266,0.0
```

判定可执行性：

```bash
python3 solve/check_trajectory.py --robot ur5e --input traj.csv \
    --time-col t --out report.csv
```

解算关节序列：

```bash
python3 solve/tcp_to_joints.py --robot ur5e --input traj.csv \
    --time-col t --out joints.csv
```

搜索整体 xyz 平移后再求解：

```bash
python3 solve/fit_trajectory.py --robot ur5e --input traj.csv \
    --time-col t --outdir out_fit --jobs 8
```

支持机器人：`franka_fr3v2`、`ur5e`、`ur7e`、`flexiv_rizon4`、`aloha_piper`、`arx5_x5`。

## 6. 报告判读

- `pipeline_report.json`：全局 processed/passed/rejected/error 统计，以及每条 episode 的路径。
- `episode_XXXX.json`：单条合并报告，包含 assessment、preprocess、transform 和可选 executability。
- `placement.json`：某个 `(arm, robot)` 的求解结论、平移量 `found_offset`、strict/replay 两套中段。
- `joints.csv`：replay 阈值下的关节角序列。
- `tcp_shifted.csv`：平移后的 TCP 轨迹，`executable` 列标记逐点可执行性。
