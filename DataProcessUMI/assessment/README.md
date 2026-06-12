# 原始数据质量验证（assessment）

本目录用于对采集的机器人原始数据（episode 级数据）做自动化质量验证。验证管线会检查
**夹爪读数、视频帧完整性、动作/末端位姿轨迹**，并通过若干跨模态交叉校验发现镜头失焦、
视频流贴错标签、左右腕部视频颠倒、静止设备上的动作漂移等问题，最终输出结构化的 JSON
报告与控制台摘要。

## 目录文件

| 文件 | 作用 |
| --- | --- |
| `validate_raw_data.py` | **主入口**。编排全部验证项，输出三层结构（result / metrics / info）的报告。 |
| `check_focus.py` | 独立的**失焦检测**工具，也被主脚本懒加载复用。 |
| `check_label_similarity.py` | 独立的**腕部视角 ↔ 触觉视频贴错标签**检测工具，也被主脚本懒加载复用。 |
| `validate_raw_data_config.json` | 各检查项的阈值配置，主脚本默认读取此文件。 |

> `check_focus.py` 与 `check_label_similarity.py` 既可单独运行（各自产出 CSV + JSON），
> 也会被 `validate_raw_data.py` 作为子模块调用，归并进统一报告。

## 期望的数据结构

每个 episode 目录形如：

```
class_name/                       # 一个“类别”目录，下含多个 episode
└── episode_0001/
    ├── metadata.json                              # fps_config、夹爪标定（calibration_config）
    ├── observation.state.gripper/data.csv         # 夹爪开合距离（米）
    ├── observation.state.raw_gripper_rotation/data.csv  # 磁编码原始旋转角（度）
    ├── actions.eef_pose/data.csv                  # 末端位姿 left_x/y/z, right_x/y/z ...
    ├── observation.image.left_wrist_view/         # 左腕广角视角
    │   ├── video.mp4
    │   └── timestamps.csv                         # 含 timestamp_ms 列
    ├── observation.image.right_wrist_view/
    ├── observation.image.left_wrist_left_tactile/   # 4 路触觉视频
    ├── observation.image.left_wrist_right_tactile/
    ├── observation.image.right_wrist_left_tactile/
    └── observation.image.right_wrist_right_tactile/
```

输入路径支持三种形式：

1. **单个 `episode_XXX` 目录** —— 只验证该 episode。
2. **类别目录**（直接包含若干 `episode_XXX` 子目录）—— 批量验证这些 episode。
3. **任意上层目录**（本身不直接包含 episode）—— **递归**搜索其下任意深度的所有
   `episode_XXX` 目录。若整棵目录树中找不到任何 episode，则**直接报错**。

第 3 种情况会保留每个 episode 相对输入根目录的子路径，并在输出目录中**镜像出相同的目录
结构**。例如输入 `input_dir`，其下 `input_dir/A/B` 含多个 episode、`input_dir/C` 含另一些
episode，则报告分别写入 `output_root/A/B/` 与 `output_root/C/`，每个目录各自带一份
`summary.validation.json`。

## 依赖

- Python 3
- `ffprobe`（FFmpeg，用于统计真实视频帧数）
- `opencv-python`、`numpy`（失焦 / 贴标签 / 运动一致性检查所需）
- `tqdm`（可选，批量验证时显示进度条）

> 注意：失焦、贴标签、运动一致性这三项依赖 OpenCV/NumPy。**若相关检查处于启用状态而缺少
> 这两个库，程序会在开始处理前直接报错并提示安装命令**（`pip install opencv-python numpy`），
> 不会继续运行。如确实不需要这些检查，可用 `--skip-focus` / `--skip-label` / `--skip-motion`
> （或 `--skip-video` / `--skip-action`）跳过后再运行。

## 使用方法

### 运行完整验证管线

```bash
# 验证单个 episode
python3 validate_raw_data.py -i /path/to/class_name/episode_0001

# 批量验证整个类别目录
python3 validate_raw_data.py -i /path/to/class_name -o outputs
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 报告输出根目录（默认 `outputs`）。 |
| `--no-reports` | 只打印控制台摘要，不写 JSON 报告。 |
| `--json PATH` | 额外把完整报告再写一份到指定路径。 |
| `--validate-config PATH` | 指定阈值配置文件（默认同目录的 `validate_raw_data_config.json`）。 |
| `--skip-gripper / --skip-video / --skip-action` | 跳过对应大类检查。 |
| `--skip-focus / --skip-label / --skip-motion` | 跳过对应的子检查。 |
| `--lap-var-threshold / --focus-frames` | 覆盖失焦阈值与采样帧数。 |
| `--label-threshold` | 覆盖触觉相似度阈值。 |
| `--action-abs-threshold` | 覆盖末端位姿坐标绝对值阈值。 |
| `--motion-video-static-threshold` | 覆盖判定视频“静止”的阈值。 |
| `--fps` | 期望帧率（默认取 `metadata.json` 的 `fps_config`，否则 30）。 |

（夹爪检查另有 `--raw-static-deg`、`--gripper-static-m`、`--min-correlation`、`--min-r2`、
`--min-gripper-m`、`--max-gripper-m`、`--recompute-tolerance-m`、`--disable-metadata-check`
等参数，详见 `--help`。）

### 单独运行子工具

```bash
# 仅做失焦检测（产出 focus_results.csv / focus_results.json）
python3 check_focus.py --root /path/to/samples --out .

# 仅做贴错标签检测（产出 label_similarity_results.csv / .json）
python3 check_label_similarity.py --root /path/to/samples --out .
```

## 验证管线

主脚本对每个 episode 依次执行下列检查，并把跨模态交叉校验的结论回写到相应维度：

### 1. 夹爪检查（gripper）

对齐 `observation.state.gripper`（开合距离，米）与
`observation.state.raw_gripper_rotation`（磁编码角度，度）同一时间戳的样本，判定夹爪映射是否合理：

- **物理范围**：开合距离需落在 `[min_gripper_m, max_gripper_m]` 内，否则 `MAPPING_BAD`。
- **静/动一致性**：
  - 角度静止 + 夹爪静止 → `STATIC_OK`（合格）
  - 角度静止但夹爪在变 → `MAPPING_BAD`
  - 角度在变但夹爪几乎不动 → `ALL_ZERO_BAD`
- **单调线性关系**：动态数据需满足 |pearson|、|spearman| ≥ 阈值且线性拟合 R² ≥ 阈值。
- **标定复算**：用 `metadata.json` 的磁标定把角度反算成距离，与记录值比较，中位绝对误差
  超过容差则 `MAPPING_BAD`。

### 2. 视频检查（video）

逐个图像流（`observation.image.*`）检查：

- **帧数一致性**：`ffprobe` 实际解码帧数须等于 `timestamps.csv` 行数。
- **时间戳异常**：检测重复 / 非单调时间戳。
- **丢帧 / 大间隔**：按帧率推断缺失的时间戳（`missing_timestamps`、`large_gaps`）。
- **重复帧比例**：重复帧占比、相邻重复帧间距是否超过 `video` 配置阈值。

### 3. 失焦检查（focus，仅腕部视角）

`check_focus.py`：均匀采样若干帧 → 缩放到 256×256 灰度 → 计算 **拉普拉斯方差**（聚焦度量，
失焦时高频细节塌缩、方差骤降）取中位数，辅以 Tenengrad（Sobel 梯度能量）。
低于 `lap_var_threshold`（默认 350）判为 `defocused`。

### 4. 贴标签检查（label，跨类别）

`check_label_similarity.py`：取每路视频**首帧**，用 HSV 色相-饱和度直方图相关性比较。四路触觉
视频外观高度相似（互相关 ~0.75–0.98），腕部广角视角则明显不同。按“**触觉亲和度**”（与各触觉
流相关性的中位数）≥ 阈值判为触觉、否则为视角；外观类别与目录名不符即判为 **跨类贴错标签**。
（仅能识别 视角 ↔ 触觉 互换；四路触觉之间的互换无法区分。）

> **标签污染会让下游检查“未检验”**：如果某一侧（左/右臂）的任意视频流被判贴错标签，说明该侧
> 的 `*_wrist_view` 与触觉目录可能被混淆——此时 `*_wrist_view` 里装的可能根本不是真实广角画面。
> 由于失焦检查（第 3 项）和运动一致性交叉校验（第 6 项）都假设参考视频是真实视角，标签不可信
> 时这两项**无法给出可靠结论**。因此：
> - 该侧 `*_wrist_view` 的**失焦检查**标为 `unverified`（不再产生 `defocused_video`）；
> - 只要有任意一侧标签不可信，**运动一致性**整体标为 `unverified`（不再产生 `swap` / `drift`）；
> - 但贴错标签本身是确凿问题，仍会把该 episode 的**数据质量判为 false**。

### 5. 动作 / 末端位姿检查（action）

读取 `actions.eef_pose/data.csv`：

- **基本完整性**：文件存在、可解析、x/y/z 有限（无 NaN/Inf）。
- **绝对值范围**：|x|、|y|、|z| 不得超过阈值（默认 1.5 m），超出即视为离群点。

### 6. 运动一致性交叉校验（motion）

以**腕部视角视频是否在动**作为“该机械臂是否真的在运动”的真值（采样帧的逐帧灰度均差：
静止 ~0.1，运动 ≥8）。把它与动作轨迹的运动量（累计 3D 路径长度 `path` + 各轴峰峰值之和
`extent`）对照：

- 某侧视频静止、但该侧动作仍在动，**且另一侧动作也在动** → **drift**（静止设备上的动作漂移，
  归为 *action* 问题）。
- 某侧视频静止、该侧动作在动、**而另一侧动作静止** → **swap**（左右腕部视频颠倒，归为
  *video* 问题）。
- 否则 → consistent。

## 输出报告

默认写入 `--output-root`（默认 `outputs`）。输出结构与输入布局保持一致：

- 输入为**类别目录 / 单个 episode**（前述第 1、2 种）：沿用旧结构
  `outputs/<class_name>/`。
- 输入为**上层目录**（前述第 3 种，递归发现）：镜像每个 episode 相对输入根目录的子路径，
  每个含 episode 的目录各自产出一份 `summary.validation.json`。

```
# 类别目录 / 单个 episode
outputs/<class_name>/
├── episode_0001.validation.json   # 每个 episode 一份
├── episode_0002.validation.json
└── summary.validation.json        # 该目录内所有 episode 的汇总

# 递归发现（input_dir/A/B、input_dir/C 各含 episode）
outputs/
├── A/B/
│   ├── episode_0001.validation.json
│   ├── episode_0002.validation.json
│   └── summary.validation.json
└── C/
    ├── episode_0003.validation.json
    └── summary.validation.json
```

> `--json` 额外输出：单个分组时写出与上面一致的扁平汇总；存在多个分组时写出包裹了各分组
> 汇总的 `groups` 组合报告。

每份 episode 报告顶部有一个 **`checks_run`**：逐项检查在该 episode 上的**结果状态**
（不是“是否启用”），取值 `true`（通过）/ `false`（不通过）/ `"unverified"`（已运行但无法确认，
例如相机/触觉标签混淆使参考视频不可信）/ `null`（被跳过）。顺序上把两项跨模态一致性检查
`camera_label_consistency`、`motion_tracker_consistency` 排在最前，其后是
`gripper` / `video` / `action` / `focus`。

报告主体采用**三层结构**，便于不同消费者按需取用：

- **`result`（第 1 层）**：三个总体质量结论 `video_quality` / `gripper_quality` /
  `pose_quality`，取值同样为 `true` / `false` / `"unverified"` / `null`。其中
  `pose_quality`：动作数据本身有问题（离群、非有限、确认的漂移）时为 `false`；动作数据干净
  但运动一致性无法确认（标签混淆）时为 `"unverified"`（无法证伪也无法证实，故不给 `true`）。
- **`metrics`（第 2 层）**：每个维度的精简指标 / 标签，如丢帧率、是否失焦、贴错的流、
  夹爪每侧判定、动作离群点数、是否漂移 / 颠倒等。
- **`info`（第 3 层）**：每项检查的完整证据（阈值、具体数值、出问题的行 / 时间戳 / 流）。

同时在控制台打印逐 episode 的可读摘要，并以
`Summary: processed=… correct=… incorrect=…` 收尾。
