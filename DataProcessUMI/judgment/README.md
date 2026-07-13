# Trajectory Judgment 轨迹质量判定

`tools/data/judgment` 用于检查 episode 中的末端轨迹是否存在工作空间漂移、单帧/短时跳变、局部高频抖动、局部区域内长期来回抖动尖峰（`cluster_jitter`）、小范围细碎尖点抖动（`micro_jitter`），以及“wrist view 视频静止但轨迹仍变化”的静态漂移，并输出每帧标签、左右臂异常段、整体质量结论和可恢复性结论。

目录名当前为 `judgment`。如果命令或文档里写成 `judgement`，应改为 `judgment`。

## 入口脚本

- `judge_trajectory.py`：主检测脚本，读取 episode 或 episode 集合并生成报告。
- `judgment_config.json`：检测阈值和质量判定阈值。
- `manual_labels.json`：人工标注回归样例。
- `validate_manual_labels.py`：用人工标注样例验证检测器行为。

## 输入

### 支持的输入路径

`judge_trajectory.py` 接收一个 `episode_*` 目录，或一个包含多个 `episode_*` 的上级目录：

```bash
python judgment/judge_trajectory.py /path/to/episode_0000 -o judgment_outputs
python judgment/judge_trajectory.py /path/to/folder_with_episodes -o judgment_outputs
```

也可以用显式参数：

```bash
python judgment/judge_trajectory.py -i /path/to/episodes -o judgment_outputs
```

### 轨迹文件

默认读取：

```text
episode_XXXX/actions.eef_pose/data.csv
```

标准双臂输入需要包含：

```text
timestamp_ms
left_x,left_y,left_z,left_r1,left_r2,left_r3,left_r4,left_r5,left_r6
right_x,right_y,right_z,right_r1,right_r2,right_r3,right_r4,right_r5,right_r6
```

如果 `actions.eef_pose/data.csv` 不是标准原始位姿格式，脚本会尝试读取已转换的 TCP/EEF 轨迹：

```text
episode_XXXX/actions.eef_pose_raw/data.csv
episode_XXXX/actions.eef_pose/data.csv
```

支持的已转换字段包括：

- 双臂 TCP：`left_tcp.x,left_tcp.y,left_tcp.z,right_tcp.x,right_tcp.y,right_tcp.z`
- 单臂 TCP：`tcp.x,tcp.y,tcp.z`
- 单臂标准坐标：`x,y,z`

### 坐标变换

当输入是标准原始双臂位姿时，脚本会先调用：

```text
tools/data/transform/ee_transform.py
```

并使用默认配置：

```text
tools/data/transform/ee_trajectory_config.json
```

把轨迹转换到 `world_eef` 坐标系后再检测。可用 `--transform-config` 指定其他转换配置：

```bash
python judgment/judge_trajectory.py /path/to/episode_0000 \
  -o judgment_outputs \
  --transform-config transform/ee_trajectory_config.json
```

## 输出

报告写到：

```text
<output-root>/report/.../episode_XXXX.json
<output-root>/report/summary.json
```

输出路径会尽量保留输入目录的相对层级。例如对 `/data/samples/task/date/umi/episode_0000` 批量运行时，报告通常位于：

```text
judgment_outputs/report/task/date/umi/episode_0000.json
```

### 控制台状态

每个 episode 会打印一行：

```text
episode_0000: OK clean -> /path/to/report/episode_0000.json
episode_0001: BAD recoverable -> /path/to/report/episode_0001.json
episode_0002: BAD unrecoverable -> /path/to/report/episode_0002.json
```

含义：

- `OK clean`：`trajectory_quality=true`。注意这里的 `clean` 是控制台简写，报告内可能是完全干净，也可能是只包含轻微局部抖动或可平滑的 `cluster_jitter` / `micro_jitter`。
- `BAD recoverable`：`trajectory_quality=false` 且 `recoverable=true`，表示有质量问题，但异常区域局部、程度可接受，理论上可通过后处理/裁剪修复。
- `BAD unrecoverable`：`trajectory_quality=false` 且 `recoverable=false`，表示漂移、严重跳变、长时间异常或异常比例过高。

静态漂移命中时控制台会显示为 `BAD recoverable`。需要区分具体原因时，看单 episode 报告中的 `quality.status` 和 `static_drift` 字段。

### 单 episode 报告字段

核心字段：

- `ok`：脚本是否成功读取并检测该 episode。读文件失败时为 `false`。
- `trajectory_quality`：最终质量结论。`true` 表示可作为成功轨迹使用。
- `quality`：质量判定详情。
- `recoverable`：当 `trajectory_quality=false` 时是否可恢复。若 `trajectory_quality=true`，通常为 `false`，因为不需要恢复。
- `recoverability`：可恢复性判定详情。
- `frame_count`：总帧数。
- `bad_frame_count`：任一侧被标为异常的帧数。
- `bad_frame_ratio`：异常帧占比。
- `sides`：每侧的质量、指标和异常段。
- `frames`：逐帧标签。
- `static_drift`：静态漂移交叉检查结果。该检查使用 wrist view 视频判断设备是否物理静止，再用轨迹 path/extent 判断对应侧轨迹是否仍在移动。

### `quality.status`

顶层 `quality.status`：

- `good`：整体通过。所有侧都干净，或只有轻微局部抖动。
- `static_drift`：检测到某侧 wrist view 视频静止但该侧轨迹移动。该状态会强制 `trajectory_quality=false`。
- `bad`：整体不通过。

每侧 `sides.<side>.quality.status`：

- `clean`：该侧无异常帧。
- `minor_jitter`：该侧有局部抖动/短跳变，或只有可平滑的 `cluster_jitter` / `micro_jitter`，但仍按成功轨迹处理。
- `static_drift`：该侧 wrist view 视频静止但轨迹移动。
- `bad`：该侧异常超过质量阈值，导致或参与导致整体失败。

### `recoverability.status`

顶层 `recoverability.status`：

- `not_needed`：`trajectory_quality=true`，不需要恢复。
- `clean`：无异常帧。
- `recoverable`：异常局部且未超过可恢复阈值。
- `unrecoverable`：不可恢复。
- `error`：输入读取或检测失败。

每侧 `sides.<side>.recoverability.status`：

- `clean`：该侧无异常帧。
- `recoverable`：该侧异常可恢复。
- `unrecoverable`：该侧异常不可恢复。

### `static_drift` 字段

`static_drift` 是 episode 级交叉检查结果，不是逐帧标签。典型结构如下：

```json
{
  "available": true,
  "has_drift": true,
  "status": "static_drift",
  "drift_sides": ["left", "right"],
  "thresholds": {
    "video_static_threshold": 4.0,
    "action_path_active": 5.0,
    "action_extent_active": 0.3
  },
  "num_sample_frames": 30,
  "video_motion": {
    "left": 1.23,
    "right": 2.34
  },
  "video_static": {
    "left": true,
    "right": true
  },
  "action_path": {
    "left": 6.12,
    "right": 8.45
  },
  "action_extent": {
    "left": 0.41,
    "right": 0.52
  },
  "action_moving": {
    "left": true,
    "right": true
  }
}
```

字段含义：

- `available`：是否成功完成静态漂移检查。视频缺失、视频不可读或 OpenCV 不可用时为 `false`。
- `has_drift`：是否检测到静态漂移。
- `status`：`static_drift`、`consistent` 或 `unavailable`。
- `drift_sides`：命中的侧，取值为 `left`、`right`。
- `video_motion`：wrist view 相邻采样帧灰度平均绝对差。
- `video_static`：对应侧视频是否低于静止阈值。
- `action_path`：对应侧轨迹累计路径长度。
- `action_extent`：对应侧轨迹 x/y/z 三轴范围和。
- `action_moving`：对应侧轨迹是否超过移动阈值。

### 异常段 `segments`

每侧报告包含：

```json
{
  "start_frame": 392,
  "end_frame": 436,
  "start_timestamp_ms": 16133,
  "end_timestamp_ms": 17600,
  "duration_ms": 1467,
  "reason_types": ["jitter_window"],
  "max_step_norm_m": 0.032442961,
  "max_accel_norm_m": 0.044424326
}
```

常见 `reason_types`：

- `workspace_drift`：轨迹超出工作空间边界。
- `static_drift`：不在逐帧 `reason_types` 中出现；它是报告级交叉检查状态，见顶层 `static_drift` 字段。
- `abrupt_step`：单帧位移或单轴位移过大。
- `abrupt_acceleration`：二阶差分过大。
- `abrupt_jerk`：三阶差分过大。
- `jitter_window`：短窗口内存在高频抖动；会要求加速度相对速度足够高，避免把速度平稳的大跨度运动误判为抖动。
- `cluster_jitter`：较长窗口内轨迹在局部区域反复往返，路径长度显著大于位移/范围。
- `micro_jitter`：短到中等窗口内存在小幅尖点、细碎反转或局部往返抖动。该检测要求窗口内存在明显非平稳速度、较高 jerk/step 或尖锐转向；平滑、均匀的大范围重复移动不会仅因轨迹重叠而命中。
- `merged_short_gap`：两个短异常段间隔很小，被合并。

### 逐帧标签 `frames`

`frames` 中每一项包含：

- `frame`：帧号。
- `timestamp_ms`：时间戳。
- `label`：整体标签，`0` 为正常，`1` 为异常。
- `smooth`：整体是否平滑。
- `sides.left/right.label`：单侧标签，`0` 为正常，`1` 为异常。
- `sides.left/right.reasons`：该侧该帧异常原因列表。

### `summary.json`

汇总报告包含：

- `processed`：处理的 episode 数。
- `ok`：成功完成检测的 episode 数。
- `normal`：`trajectory_quality=true` 的 episode 数。
- `abnormal`：`trajectory_quality=false` 的 episode 数。
- `static_drift`：检测到静态漂移的 episode 数。
- `recoverable`：`recoverable=true` 的 episode 数。
- `unrecoverable`：`trajectory_quality=false` 且 `recoverable` 不是 `true` 的 episode 数。
- `episodes`：每个 episode 的简要结果。

`episodes` 中每项会包含：

- `trajectory_quality`
- `recoverable`
- `static_drift`
- `static_drift_sides`
- `bad_frame_count`
- `bad_frame_ratio`
- `report`

## 算法流程

1. 递归找到输入路径下的 `episode_*`。
2. 读取 `actions.eef_pose/data.csv`。
3. 如输入是原始双臂位姿，按 `transform/ee_trajectory_config.json` 转到 `world_eef`。
4. 对每侧位置序列分别检测：
   - 工作空间漂移；
   - 单帧/短时突变；
   - 局部高频抖动。
5. 如果 wrist view 视频存在，额外检查静态漂移：视频静止但对应侧轨迹移动。
6. 合并短间隔异常段，过滤过短异常段。
7. 生成每侧 `segments`、`metrics` 和逐帧标签。
8. 汇总左右侧和静态漂移结果，生成 episode 级 `trajectory_quality`。
9. 如果质量失败，进一步判断 `recoverable`。静态漂移固定视为 `recoverable=true`。
10. 写出单 episode JSON 和汇总 `summary.json`。

## 检测逻辑

### 工作空间漂移

直接检查转换后的 `world_eef` 坐标是否越界：

- `x < workspace_bounds.x_min`
- `x > workspace_bounds.x_max`
- `abs(y) > workspace_bounds.y_abs_max`
- `z < workspace_bounds.z_min`
- `z > workspace_bounds.z_max`

对应原因类型为 `workspace_drift`。

### 突变检测

基于位置序列计算：

- `step_norm`：相邻帧位移范数。
- `axis_step`：相邻帧单轴最大位移。
- `accel_norm`：二阶差分范数。
- `jerk_norm`：三阶差分范数。

超过阈值时，将附近 `expand_frames` 范围标为异常。

### 抖动检测

对每个中心帧取一个局部窗口，计算：

- `accel_rms_m`：窗口内加速度 RMS。
- `step_rms_m`：窗口内步长 RMS。
- `path_ratio`：窗口路径长度 / 起终点直线距离。
- `axis_reversals`：各轴运动方向反转次数。
- `residual_peak_m`：相对局部滚动中位数的最大残差。

满足高频运动、残差尖峰串或粗糙运动条件时，标为 `jitter_window`。为避免平稳运动中的自然轨迹重叠被误检，窗口还会检查 `accel_rms / step_rms` 是否达到配置中的粗糙度下限。

### 静态漂移检测

静态漂移检测对齐 assessment 中的 motion-consistency 逻辑，用来发现：

```text
wrist_view 视频几乎不动，但该侧轨迹仍明显变化
```

检测步骤：

1. 读取：

```text
observation.image.left_wrist_view/video.mp4
observation.image.right_wrist_view/video.mp4
```

2. 均匀采样若干帧，将相邻采样帧转成灰度后计算平均绝对差：

```text
video_motion = mean(abs(gray_t - gray_{t-1}))
```

3. 若 `video_motion < static_drift.video_static_threshold`，认为该侧 wrist view 视频静止。
4. 对该侧已转换轨迹计算：

- `action_path`：累计 3D 路径长度。
- `action_extent`：x/y/z 三轴 peak-to-peak 范围之和。

5. 若视频静止，并且 `action_path > action_path_active` 或 `action_extent > action_extent_active`，则该侧标为 `static_drift`。

静态漂移命中时：

- 顶层 `static_drift.has_drift=true`
- 顶层 `static_drift.drift_sides` 给出漂移侧
- 顶层 `trajectory_quality=false`
- 顶层 `quality.status=static_drift`
- 顶层 `recoverable=true`
- 顶层 `recoverability.status=recoverable`

如果视频缺失、不可读或 OpenCV 不可用，`static_drift.available=false`，不会影响原有轨迹质量判定。

## 参数位置与意义

主要参数在：

```text
tools/data/judgment/judgment_config.json
```

可通过 `--config` 传入一个 JSON 覆盖默认配置。覆盖采用 deep merge，只需写需要调整的字段：

```bash
python judgment/judge_trajectory.py /path/to/episode_0000 \
  -o judgment_outputs \
  --config my_judgment_config.json
```

### `workspace_bounds`

工作空间边界：

- `x_min` / `x_max`：x 轴允许范围。
- `y_abs_max`：y 轴绝对值上限。
- `z_min` / `z_max`：z 轴允许范围。

越界会产生 `workspace_drift`。

### `abrupt_change`

突变检测阈值：

- `step_norm_m`：相邻帧位移范数阈值。
- `axis_step_m`：相邻帧单轴位移阈值。
- `accel_norm_m`：二阶差分阈值。
- `jerk_norm_m`：三阶差分阈值。
- `expand_frames`：检测到突变后向前/向后扩展标记的帧数。

### `jitter`

局部抖动检测阈值：

- `window_frames`：局部窗口长度。
- `median_window_frames`：滚动中位数窗口长度，用于计算残差。
- `residual_norm_m`：局部残差峰值阈值。
- `accel_rms_m`：窗口加速度 RMS 阈值。
- `step_rms_m`：窗口步长 RMS 阈值。
- `path_ratio`：路径长度与直线距离比值阈值。
- `min_axis_reversals`：最少方向反转次数。
- `small_motion_m`：低于该幅度的轴向运动不计入方向反转。
- `merge_gap_frames`：相邻异常段间隔不超过该值时合并。
- `min_segment_frames`：合并后异常段短于该值时过滤。

### `static_drift`

静态漂移检测阈值：

- `video_static_threshold`：wrist view 视频帧差低于该值时认为视频静止。默认 `4.0`。
- `action_path_active`：轨迹累计路径长度超过该值时认为轨迹移动。默认 `5.0`。
- `action_extent_active`：轨迹三轴范围和超过该值时认为轨迹移动。默认 `0.3`。
- `num_sample_frames`：视频均匀采样帧数。默认 `30`。

### `quality`

质量判定阈值。它决定异常是否只是 `minor_jitter`，还是导致 `trajectory_quality=false`：

- `minor_jitter_max_episode_bad_frame_ratio`：episode 级异常帧比例上限。
- `minor_jitter_max_bad_frame_ratio`：单侧异常帧比例上限。
- `minor_jitter_max_segment_duration_ms`：单侧最长异常段时长上限。
- `minor_jitter_max_step_norm_m`：轻微抖动允许的最大单帧位移。
- `minor_jitter_max_axis_step_m`：轻微抖动允许的最大单轴位移。
- `minor_jitter_max_accel_norm_m`：轻微抖动允许的最大加速度。
- `minor_jitter_max_jerk_norm_m`：轻微抖动允许的最大 jerk。
- `minor_jitter_allowed_reason_types`：仍可视为轻微局部抖动的原因类型白名单。
- `moderate_metric_min_episode_bad_frame_ratio`：当 episode 异常帧比例超过该值时，启用更严格的 moderate 指标判定。
- `moderate_step_norm_m` / `moderate_accel_norm_m` / `moderate_jerk_norm_m`：moderate 判定阈值。

### `recoverable`

质量失败后的可恢复性判定：

- `max_bad_frame_ratio`：可恢复允许的最大异常帧比例。
- `max_segment_duration_ms`：可恢复允许的最长异常段时长。
- `workspace_drift_blocks_recovery`：工作空间漂移是否直接阻断恢复。
- `severe_step_norm_m`：严重单帧位移阈值。
- `severe_axis_step_m`：严重单轴位移阈值。
- `severe_accel_norm_m`：严重加速度阈值。
- `severe_jerk_norm_m`：严重 jerk 阈值。

## 人工标注回归

人工样例写在：

```text
tools/data/judgment/manual_labels.json
```

每个样例包含：

- `episode_path`：样例 episode。
- `expected_trajectory_quality`：期望质量结论。
- `expected_recoverable`：期望可恢复性。
- `segments`：人工标注的异常帧段。

运行检测并验证：

```bash
python judgment/validate_manual_labels.py --run-detector -o outputs/judgment_validation
```

如果已有报告，只验证不重跑：

```bash
python judgment/validate_manual_labels.py -o outputs/judgment_validation
```

## 常见解读

- `trajectory_quality=true` 不等于完全无异常。报告可能仍包含少量 `segments`，但它们满足 `minor_jitter` 条件。
- `quality.status=static_drift` 表示视频证据显示设备静止，但轨迹仍移动；该状态会让质量失败，但可恢复性固定为 `recoverable=true`。
- `recoverable=false` 在 `trajectory_quality=true` 时通常表示“不需要恢复”，不是不可恢复。
- 如果想看具体局部跳变/抖动区域，优先看 `sides.left.segments` 和 `sides.right.segments`。
- 如果只想快速筛选失败样本，看 `summary.json` 中每个 episode 的 `trajectory_quality` 和 `recoverable`。
