# DataProcessUMI — UMI 数据预处理管线

对原始 UMI / 机器人 episode 数据做**有效性评估 → 轨迹平滑清洗 → 世界坐标变换**的
一体化预处理管线，产出可直接用于下游训练 / IK 解算的干净数据与逐条质量报告。

```
assessment（有效性评估 / 门禁）→ preprocess（轨迹突变检测 + 插值 / 裁剪 / 拒绝）→ transform（世界基座 EEF 变换 + 腕部视频翻转）
```

## 目录结构

```
DataProcessUMI/
├── pipeline/                 # 一键编排入口（推荐使用）
│   └── run_pipeline.py       #   assessment → preprocess → transform 逐条串联
├── assessment/               # 阶段 1：数据有效性评估
│   ├── validate_raw_data.py  #   主入口：夹爪 / 视频 / 末端位姿 + 跨模态交叉校验
│   ├── check_focus.py        #   失焦检测（被 validate_raw_data 调用，也可独立运行）
│   ├── check_label_similarity.py  # 视频流贴错标签检测（同上）
│   └── validate_raw_data_config.json  # 评估阈值配置
├── preprocess/               # 阶段 2：轨迹平滑与清洗
│   ├── preprocess_trajectory.py    #   主入口：按分类插值修复 / 裁剪 / 拒绝
│   ├── smooth_assessment.py        #   轨迹突变检测与五类分类（也可独立出报告）
│   ├── preprocess_config.json      #   处理参数（裁剪余量、最短保留帧数等）
│   └── smooth_assessment_config.json  # 突变检测阈值
├── transform/                # 阶段 3：世界坐标变换
│   ├── transform_episode_w_world_base.py  # 主入口：位姿 CSV 改写 + 腕部视频翻转
│   ├── ee_transform.py             #   变换数学（旋转序列 / 偏移 / 投影）
│   ├── ee_trajectory_config.json   #   变换参数
│   └── visualize_episode_w_world_base.py  # 变换前后轨迹对比可视化（浏览器）
└── requirements.txt
```

各目录内附有更详细的模块 README（参数、报告字段、算法说明）。

## 环境依赖

- Python ≥ 3.8
- `pip install -r requirements.txt`（numpy、scipy、opencv-python；tqdm 可选，仅进度条）
- 系统需有 **ffmpeg / ffprobe**（视频帧统计、裁剪、翻转）：
  `sudo apt install ffmpeg`

## 输入数据格式

输入可以是单条 episode 目录、一个类别目录、或包含多个类别的数据集根目录
（递归发现，输出镜像类名层级）。每条 episode 目录形如：

```
episode_XXXX/
├── metadata.json                       # 含 fps、磁编码标定等元信息
├── checksums.sha256
├── actions.eef_pose/data.csv           # 末端位姿动作（x,y,z + 6D 旋转）
├── observation.state.eef_pose/data.csv
├── observation.state.gripper/data.csv
├── observation.image.<视角>/           # left/right_wrist_view 及各触觉流
│   ├── video.mp4
│   └── timestamps.csv
└── meta/episode.json
```

## 快速开始（一键管线）

```bash
# 处理一个类别目录（或数据集根目录 / 单条 episode 均可）
python3 pipeline/run_pipeline.py /path/to/class_name -o pipeline_out

# 跳过有效性评估（全部放行），并保留中间产物便于排查
python3 pipeline/run_pipeline.py /path/to/class_name -o pipeline_out \
    --skip-assessment --keep-intermediate
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 输出根目录（默认 `pipeline_out`） |
| `--suffix` | 输出类名后缀（默认 `_w_world_base`） |
| `--overwrite` | 覆盖已存在的输出根目录 |
| `--skip-assessment` | 跳过阶段 1 评估与门禁（全部放行） |
| `--keep-intermediate` | 保留 `.work/` 中间产物（评估报告 + 清洗后 episode） |
| `--fps` | 覆盖帧率（默认读 metadata，否则 30） |
| `--preprocess-config` / `--smooth-config` / `--transform-config` | 各阶段配置文件覆盖 |
| `--assessment-args` | 透传给 `validate_raw_data.py` 的额外参数，如 `"--skip-focus --skip-motion"` |

**重要原则：管线只读输入、向输出目录写结果，从不修改或删除原始数据。**
被拒绝的 episode 只是不进入输出 `data/`，原始数据保持原样。

## 输出结构

```
<output-root>/
├── data/                                  # 仅 status=passed 的可用数据
│   └── <class_name>_w_world_base/
│       └── episode_XXXX/                  # 已清洗 + 世界坐标变换后的 episode
├── report/
│   ├── <class_name>/episode_XXXX.json     # 每条 episode 的合并报告
│   └── pipeline_report.json               # 全局统计报告
└── .work/                                 # 中间产物（默认删除，--keep-intermediate 保留）
```

每条 episode 最终落到三态之一：

| `status` | 含义 | 是否产出数据 |
| --- | --- | --- |
| `passed` | 通过评估门禁，清洗成功，坐标变换成功 | 有 |
| `rejected` | 被门禁拦截，或轨迹不可恢复 / 裁剪后过短 | 无 |
| `error` | 某一步抛异常（数据损坏、依赖缺失等） | 无 |

合并报告写明 `failed_stage`（停在哪一步）、`reason`（具体原因）、评估各维度结论、
轨迹分类标签、平滑 / 裁剪操作明细与变换记录，可解释、可追溯。
被输出的 episode 在 `metadata.json` 中追加 `preprocessing` / `umi_transform_*` 溯源块，
并重算 `checksums.sha256` 保证与写出字节一致。

## 各阶段说明

### 阶段 1：有效性评估（assessment）

入口 `assessment/validate_raw_data.py`。逐 episode 检查：

- **夹爪**：开合距离与磁编码角度的物理范围、静/动一致性、单调线性关系、标定复算误差。
- **视频**：ffprobe 实际帧数 vs `timestamps.csv` 行数、重复 / 非单调时间戳、丢帧、重复帧比例。
- **失焦**（腕部视角）：采样帧灰度拉普拉斯方差，低于阈值判失焦。
- **贴标签**：首帧 HSV 直方图区分“腕部视角 vs 触觉”，外观与目录名不符判贴错。
- **末端位姿**：`actions.eef_pose` 可解析、坐标有限、绝对值不超阈值（默认 1.5 m）。
- **运动一致性**（跨模态）：以腕部视频运动为真值对照动作运动量，检出左右视频颠倒（swap）
  与静止设备上的动作漂移（drift）。

**门禁规则**：夹爪问题与视频掉帧类问题**放行**（如实记录）；失焦、贴错标签、左右颠倒、
缺文件、时间戳非单调及任何动作 / 位姿问题**拦截**（episode 记为 `rejected`）。
可容忍问题集合见 `pipeline/run_pipeline.py` 中 `TOLERABLE_VIDEO_PROBLEMS`，可按需增删。

独立运行：

```bash
python3 assessment/validate_raw_data.py /path/to/class_name -o reports/
```

### 阶段 2：轨迹平滑与清洗（preprocess）

入口 `preprocess/preprocess_trajectory.py`，突变检测复用 `preprocess/smooth_assessment.py`。

检测模型（逐设备，仅 x/y/z）：过去 `window_s`（0.5 s）窗口内位移超过
`jump_displacement_m`（0.35 m）记为突变帧 → 连续突变帧成段 → 若在 `recover_window_s`（1.0 s）
内回到锚点 `return_tolerance_m`（0.35 m）以内则**可恢复**，否则**不可恢复**。
据此给整段轨迹打五类标签并归并为四种动作：

| 标签 | 动作 |
| --- | --- |
| `smooth`（全程平滑） | 原样输出（passthrough） |
| `recoverable`（仅可恢复突变） | PCHIP 插值修复（位置 + 6D 旋转，旋转后重正交化） |
| `middle_smooth` / `middle_recoverable`（不可恢复段只在首尾且每段 < 3 s） | 先插值中部，再裁掉首尾——动作 / 状态 / 夹爪 / 视频 / 时间戳全部按同一帧窗口对齐裁剪 |
| `unrecoverable`（中部不可恢复，或首尾不可恢复段 ≥ 3 s） | 拒绝，不输出 |

裁剪后剩余帧数 < `min_kept_frames`（默认 30）同样判不可用。阈值均可在
`smooth_assessment_config.json` / `preprocess_config.json` 中调整。

独立运行：

```bash
# 只出分类报告，不动数据
python3 preprocess/smooth_assessment.py /path/to/class_name -o smooth_reports/
# 实际清洗产出
python3 preprocess/preprocess_trajectory.py /path/to/class_name -o cleaned/
```

### 阶段 3：世界坐标变换（transform）

入口 `transform/transform_episode_w_world_base.py`，变换数学在 `transform/ee_transform.py`。

- 依 `ee_trajectory_config.json`（旋转序列、位置偏移、`local_ee_projection`、
  `world_projection`）把 `observation.state.eef_pose` 与 `actions.eef_pose` 的 CSV
  改写到**世界基座 EEF 坐标系**。
- 翻转腕部视频（`hflip,vflip`）使画面与新坐标系一致。
- 更新 metadata 的 `umi_transform_*` 字段并重算校验和。

独立运行与可视化：

```bash
python3 transform/transform_episode_w_world_base.py /path/to/episode_XXXX -o transformed/
python3 transform/visualize_episode_w_world_base.py /path/to/episode_XXXX   # 浏览器对比
```

## 验证

移植后已通过完整冒烟测试：对真实 episode 运行
`python3 pipeline/run_pipeline.py <episode> -o <out>`，
assessment → preprocess → transform 全流程 `passed`，输出数据与报告齐全。
