# 一键式数据管线（pipeline / run_pipeline）

把原始 UMI / 机器人 episode **逐条**串起数据质量处理，并可选接入 IK / 可执行性求解：

```
assessment → preprocess → transform [→ executability]
```

- `assessment`：有效性评估与门禁，检查夹爪、视频、失焦、标签、末端位姿与跨模态运动一致性。
- `preprocess`：轨迹突变检测，按结果原样通过、插值修复、首尾裁剪，或拒绝不可恢复 episode。
- `transform`：把 tracker 位姿变换到 world-base EEF 坐标系，并翻转腕部视频。
- `executability`：可选阶段。对已变换 episode 调用 `executability/solve_executability.py`，
  使用 `solve/` 的 Pinocchio/Coal IK 栈，在各机器人本体上搜索可执行摆放、输出关节轨迹与可执行中段。

默认只跑前三步；加 `--run-executability` 后才运行 IK / 可执行性求解，因为该阶段依赖机器人模型、
`pin` 和碰撞检查，耗时明显高于清洗与坐标变换。

## 输出布局

```
<output-root>/
├── data/
│   └── <class_name>_w_world_base/
│       └── episode_XXXX/                  # 已清洗 + world-base 变换后的 episode
├── report/
│   ├── <class_name>/
│   │   ├── episode_XXXX.json              # 每条 episode 的合并报告
│   │   └── episode_XXXX/executability/    # 可选：IK/可执行性明细
│   └── pipeline_report.json
└── .work/                                 # 中间产物，默认删除
```

合并报告包含 `assessment`、`classification`、`smoothing`、`transform`，以及可选的
`executability` 段。`executability.summary` 是 `out_exec/summary.json` 的内容摘要，
每个 `(arm, robot)` 下会记录是否存在可执行中段、平移量、strict/replay 两套阈值结果与输出文件路径。

## 使用方法

```bash
# 处理一个类别目录、数据集根目录或单条 episode
python3 pipeline/run_pipeline.py /path/to/class_name -o pipeline_out

# 跳过 assessment，保留中间产物便于排查
python3 pipeline/run_pipeline.py /path/to/class_name -o pipeline_out \
    --skip-assessment --keep-intermediate

# 跑完整管线，并在已变换 episode 上做 IK / 可执行性求解
python3 pipeline/run_pipeline.py /path/to/class_name -o pipeline_out \
    --run-executability --ik-robots flexiv_rizon4 ur5e --ik-arm both --ik-jobs 8
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 输出根目录，默认 `pipeline_out`。 |
| `--suffix` | 输出类名后缀，默认 `_w_world_base`。 |
| `--overwrite` | 输出根目录已存在时先清空。 |
| `--skip-assessment` | 跳过第 1 步门禁，全部 episode 进入 preprocess。 |
| `--keep-intermediate` | 保留 `.work/` 中间产物。 |
| `--fps` | 覆盖帧率，默认读 metadata，否则 30。 |
| `--preprocess-config` / `--smooth-config` / `--transform-config` | 覆盖对应阶段配置。 |
| `--assessment-args "..."` | 透传给 `assessment/validate_raw_data.py`。 |
| `--run-executability` | transform 后运行 episode 级 IK / 可执行性求解。 |
| `--ik-robots ...` | 可执行性求解的机器人子集，默认 `solve/robots.py` 注册的全部机器人。 |
| `--ik-arm left|right|both` | 求解左臂、右臂或双臂，默认 `both`。 |
| `--ik-source action|state` | 使用动作目标或观测状态 eef_pose，默认 `action`。 |
| `--ik-max-points N` | 可执行性全校验最多抽稀到 N 点，`0` 表示不抽稀。 |
| `--ik-min-segment N` | 判定可执行所需最短连续中段长度，按抽稀点计。 |
| `--ik-jobs N` | IK/碰撞校验并行进程数。 |
| `--ik-samples N` | 每个机器人工作空间采样点数。 |
| `--ik-extra-args "..."` | 透传给 `executability/solve_executability.py` 的高级参数。 |

## 通过与失败

- `passed`：assessment 门禁通过，preprocess 产出可用 episode，transform 成功。若开启
  `--run-executability`，IK 结果写入报告；是否存在可执行中段不改变 `passed` 状态。
- `rejected`：assessment 门禁拦截，或 preprocess 判定轨迹不可恢复 / 裁剪后过短。
- `error`：某阶段抛异常。若开启 `--run-executability` 且 IK 依赖缺失或求解过程异常，
  该条 episode 的 `failed_stage` 为 `executability`。

assessment 门禁策略：夹爪问题和视频掉帧类问题放行并记录；失焦、贴错标签、左右颠倒、
缺文件、时间戳非单调，以及任何 action/pose 问题会拦截。

## 依赖

基础三阶段依赖 `numpy`、`scipy`、`opencv-python`、`ffmpeg/ffprobe`。开启 executability 还需要：

```bash
pip install pin
```

机器人模型和 `package://` 网格解析来自同仓 `resources/`；详情见 `resources/README.md`。
