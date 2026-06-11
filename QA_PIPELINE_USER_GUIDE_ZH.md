# QA Pipeline 用户指南

最后更新：2026-06-10

## 目的

QA Pipeline 用于检查机器人和 UMI 的 episode 文件夹，并将每个 episode 分类为：

```text
pass
warning
needs_review
fail
```

当前系统以生成报告为主。主 QA 运行过程中不会删除源数据、不会将 episode 移入隔离区，也不会裁剪视频。这些操作必须作为单独的、经过复核的步骤执行。

## 主入口

从仓库根目录运行流水线：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/test_run/qa.db \
  --output-dir outputs/test_run \
  --phases 1,2,3 \
  --max-episodes 10 \
  --force-rerun \
  --run-id test-run-001
```

服务器或较大的本地运行可使用 workers：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --db-path outputs/server_test/qa.db \
  --output-dir outputs/server_test \
  --phases 1,2,3,4,5 \
  --workers 8 \
  --force-rerun \
  --run-id server-test-001
```

`--workers` 目前会加速第 4 阶段和第 5 阶段。第 1、2、3 阶段基本仍为顺序执行。

## 输入

输入根目录可以是一个或多个包含 episode 目录的文件夹：

```text
<root>/<task>/<date>/<operator>/episode_...
```

扫描器会查找名称以 `episode_` 开头的文件夹。每个 episode 预期包含元数据、模态文件夹、CSV 文件、图像时间戳和视频，例如：

```text
episode_0001/
  metadata.json
  observation.state.joint_position/data.csv
  actions.joint_position/data.csv
  observation.image.third_view/timestamps.csv
  observation.image.third_view/video.mp4
```

## 输出

常规输出目录包含：

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
qa.db
```

启用实时监控时，每次运行还会创建：

```text
outputs/<run>/runs/<run-id>/
  run_status.json
  phase_status.jsonl
  issue_events.jsonl
  episode_issues.csv
  live_summary.md
  final/
    quality_report.csv
    quality_findings.jsonl
    quality_summary.md
    dashboard.html
```

`quality_report.csv` 每个 episode 一行。`quality_findings.jsonl` 和 `episode_issues.csv` 每个具体问题一行。

## Dashboard

运行结束后，打开：

```text
<output-dir>/dashboard.html
```

或运行目录中的副本：

```text
<output-dir>/runs/<run-id>/final/dashboard.html
```

如需从服务器访问：

```bash
cd outputs/server_test
python3 -m http.server 8080
```

然后打开：

```text
http://<server-ip>:8080/dashboard.html
```

Dashboard 展示：

- episode 总数；
- `fail`、`needs_review`、`warning` 和 `pass` 数量；
- 主要问题类型；
- 按阶段统计的问题数量；
- 可筛选的 episode 表；
- 可筛选的具体问题表。

## 运行中的实时状态

查看面向人工阅读的实时摘要：

```bash
watch -n 2 cat outputs/server_test/runs/server-test-001/live_summary.md
```

实时查看记录到的问题：

```bash
tail -f outputs/server_test/runs/server-test-001/issue_events.jsonl
```

读取便于机器处理的运行状态：

```bash
cat outputs/server_test/runs/server-test-001/run_status.json
```

## 状态判定方式

每个阶段会产生零个或多个 finding。每个 finding 包含：

```text
phase
check_name
severity
status
message
details
```

严重程度取值：

```text
critical
major
minor
info
```

阶段状态根据 findings 按以下逻辑判定：

1. 任意 `critical` finding -> 阶段状态为 `fail`。
2. 任意 `major` finding 且自身状态为 `fail` -> 阶段状态为 `fail`。
3. 任意 finding 的状态为 `needs_review` -> 阶段状态为 `needs_review`。
4. 剩余 finding 中存在 `major` 或 `minor` -> 阶段状态为 `warning`。
5. 没有有意义的 finding -> 阶段状态为 `pass`。

最终 episode 状态合并所有已完成阶段的状态：

1. 任意阶段为 `fail` -> 最终状态为 `fail`。
2. 否则任意阶段为 `needs_review` -> 最终状态为 `needs_review`。
3. 否则任意阶段为 `warning` -> 最终状态为 `warning`。
4. 否则最终状态为 `pass`。

重要：如果某个 episode 在较早的已完成阶段失败，后续阶段会跳过该 episode。这样可以避免在已经明显不可用的 episode 上浪费计算资源。

## 第 1 阶段：结构和元数据

文件：

```text
QA_Pipeline/scripts/pipeline/phase1_metadata.py
```

目的：

检查 episode 是否具有预期的文件夹名、元数据、模态文件夹、必需文件和质量标签。

通过条件：

- episode 文件夹名称以 `episode_` 开头；
- `metadata.json` 存在且为合法 JSON；
- 必需元数据字段存在且有效；
- 元数据中的模态有对应文件夹；
- 必需文件存在且非空；
- 存在质量标签。

主要非通过情况：

| Check | 含义 | 状态影响 |
| --- | --- | --- |
| `episode_folder_name` | 文件夹名称不是以 `episode_` 开头 | fail |
| `metadata_exists` / `metadata_valid_json` | 元数据缺失或无效 | fail |
| `required_metadata_field` | 必需元数据字段缺失或无效 | fail |
| `modality_folder_missing` | 元数据中声明的模态文件夹缺失 | fail |
| `required_modality_file_missing` | 必需的 `data.csv`、`timestamps.csv` 或 `video.mp4` 缺失 | fail |
| `required_modality_file_empty` | 必需文件存在但为空 | fail |
| `parent_path_structure` | 路径不像 `<task>/<date>/<operator>/<episode>` | warning |
| `checksum_manifest_missing` | `.checksum_manifest` 缺失 | warning |
| `quality_labels_missing` | `quality.labels` 缺失或为空 | warning |

## 第 2 阶段：时长和数量一致性

文件：

```text
QA_Pipeline/scripts/pipeline/phase2_duration.py
```

目的：

检查元数据中的时长和帧数一致性、CSV 行数、图像时间戳行数、视频与动作长度对齐，以及同任务组内的时长离群值。

通过条件：

- `duration_seconds` 为正数；
- `total_frames` 为正数；
- `duration_seconds * FPS` 与 `total_frames` 大致匹配；
- 图像时间戳行数与 `total_frames` 大致匹配；
- 状态 CSV 行数与预期行数大致匹配；
- 图像时间戳行数和主动作行数的差值不超过配置的绝对阈值；
- 时长不是任务级别的极端离群值。

主要非通过情况：

| Check | 条件 | 状态影响 |
| --- | --- | --- |
| `duration_not_positive` | `duration_seconds` 缺失或不为正数 | fail |
| `total_frames_not_positive` | `total_frames` 缺失或不为正数 | fail |
| `duration_frames_fps_inconsistent` | `total_frames` 与 `duration_seconds * fps` 的差异超过 10% | fail |
| `timestamps_unreadable` | 图像 `timestamps.csv` 无法读取 | fail |
| `timestamps_row_count_mismatch` | 图像时间戳行数与 `total_frames` 的差异超过 10% | fail |
| `state_csv_row_count_mismatch` | 状态 CSV 行数与预期行数的差异超过 15% | warning |
| `video_action_length_mismatch` | 图像时间戳行数和主动作行数的差值超过配置阈值，默认 3 | fail |
| `duration_task_outlier` | 同任务组内时长 IQR 距离大于 3 | needs_review |
| `duration_absolute_too_short` | 时长小于任务中位数的 20% | fail |
| `duration_absolute_too_short` | 时长小于任务中位数的 40% | needs_review |
| `duration_absolute_too_long` | 时长大于任务中位数的 250% | needs_review |

视频和动作长度差异阈值配置在：

```json
phase2_duration.length_alignment.max_video_action_difference
```

## 第 3 阶段：时间戳、FPS 和丢帧检查

文件：

```text
QA_Pipeline/scripts/pipeline/phase3_timestamp.py
```

目的：

检查图像时间戳质量、丢帧、实际 FPS、原始/处理后时间戳一致性，以及不同图像模态之间的开始/结束对齐。状态和动作时间戳检查由第 5 阶段处理。

通过条件：

- 图像时间戳文件可读取；
- 时间戳严格递增；
- 重复时间戳比例很低或为零；
- 丢帧比例和连续丢帧在配置阈值内；
- 实际 FPS 接近预期 FPS；
- 图像模态的开始和结束时间在对齐阈值内。

主要非通过情况：

| Check | 条件 | 状态影响 |
| --- | --- | --- |
| `timestamps_unreadable` | 时间戳来源缺失或无法读取 | fail |
| `timestamps_not_monotonic` | 违规比例 >=5% | fail |
| `timestamps_not_monotonic` | 违规比例 >=1% 且 <5% | needs_review |
| `timestamps_not_monotonic` | 违规比例 <1% | warning |
| `duplicate_timestamps` | 与单调性检查相同的比例规则 | fail / needs_review / warning |
| `frame_drop_ratio` | 丢帧比例超过阈值 | fail |
| `frame_drop_consecutive` | 连续丢帧超过阈值 | fail |
| `abnormal_fps_loss` | 实际 FPS 低于预期且超过阈值，默认 10% | fail |
| `abnormal_fps_gain` | 实际 FPS 高于预期且超过阈值，默认 10% | warning |
| `timestamps_raw_inconsistency` | 原始和处理后时间戳行数差异超过 2 | warning |
| `modality_alignment_start` / `modality_alignment_end` | 开始/结束时间戳跨度超过 500 ms | fail |
| `frequency_group_outlier` | 实际 FPS 是 task+robot 组内 IQR 离群值 | needs_review |
| `consecutive_drops_outlier` | 最大连续丢帧是 IQR 离群值，或在小样本组中超过兜底 warning 阈值 | needs_review / warning |

配置阈值：

```json
phase3_timestamp.abnormal_fps.loss_fail_ratio = 0.10
phase3_timestamp.abnormal_fps.gain_warning_ratio = 0.10
phase3_timestamp.frame_drops.normal_video_drop_ratio_fail = 0.15
phase3_timestamp.frame_drops.tactile_video_drop_ratio_fail = 0.20
phase3_timestamp.frame_drops.max_consecutive_fail = 25
phase3_timestamp.frame_drops.max_consecutive_warn = 10
```

## 第 4 阶段：视频健康检查

文件：

```text
QA_Pipeline/scripts/pipeline/phase4_video.py
```

目的：

打开视频文件，检查视频元数据，采样帧，并检测明显的视觉损坏。

依赖：

```text
opencv-python-headless
```

如果未安装 OpenCV，第 4 阶段会在写入 episode QA 结果前停止。这是环境/配置失败，不是 episode 质量 finding。

通过条件：

- 每个视频可以成功打开；
- 视频帧数可读取并且与元数据大致匹配；
- 视频时长与元数据时长大致匹配；
- 分辨率与元数据/配置兼容；
- 采样帧不是大面积黑屏或白屏；
- 采样帧不是冻结画面；
- ARX 腕部视角不是两路都静止。

主要非通过情况：

| Check | 条件 | 状态影响 |
| --- | --- | --- |
| `video_not_openable` | `video.mp4` 无法打开 | fail |
| `video_frame_count_unreadable` | 帧数不可用或 <=0 | fail |
| `video_frame_count_mismatch` | 视频帧数与元数据差异超过 10% | fail |
| `video_duration_mismatch` | 视频时长与元数据差异超过 10% | warning |
| `video_resolution_mismatch` | 分辨率与预期相机分辨率不一致 | warning |
| `video_black_frames` / `video_white_frames` | 发现异常采样帧 | needs_review；如果大多数采样异常则 fail |
| `video_frozen` | 采样帧看起来冻结 | fail |
| `both_wrist_views_still` | 对 ARX5，两路腕部视角相机看起来都静止 | fail |

## 第 5 阶段：机器人状态和运动合理性

文件：

```text
QA_Pipeline/scripts/pipeline/phase5_robot_state.py
```

目的：

检查关节/动作/状态 CSV 数据中的非数值、时间戳问题、关节限位、夹爪限位、夹爪重映射需求、突变步长、速度、加速度、抖动、末端执行器跳变和操作员静止。

通过条件：

- 数值列可正常解析；
- 时间戳有效且递增；
- 关节和夹爪值保持在配置的机器人范围内；
- 逐帧步长、速度、加速度和抖动低于阈值；
- 没有过长的操作员静止；
- 末端执行器位姿步长在配置阈值内。

主要非通过情况：

| Check | 含义 | 状态影响 |
| --- | --- | --- |
| `csv_not_parseable` | `data.csv` 无法读取或解析 | fail |
| `joint_nan_inf` | 运动列中存在 NaN、Inf 或无法解析的值 | fail |
| `timestamps_missing_or_unparseable` | `timestamp_ms` 缺失或不可用 | fail |
| `timestamps_not_monotonic` | 与第 3 阶段相同的比例规则 | fail / needs_review / warning |
| `joint_out_of_limits` | 关节值超出机器人限位 | needs_review |
| `gripper_out_of_limits` | 夹爪值超出机器人限位 | needs_review |
| `gripper_mean_too_low_remap_needed` | 平均夹爪距离低于阈值，默认 0.005 m | needs_review |
| `joint_step_too_large` | 逐帧关节步长过大 | needs_review |
| `gripper_step_too_large` | 逐帧夹爪步长过大 | needs_review |
| `joint_velocity_exceeded` | 关节速度 p99 超过阈值 | needs_review |
| `joint_acceleration_high` | 加速度 p99 超过阈值 | warning |
| `jitter_high` | 抖动分数超过 warning/fail 阈值 | warning 或 fail |
| `operator_standstill` | 静止片段超过 4 秒缓冲区 | warning |
| `operator_standstill_excessive` | 总超额静止时间超过 episode 时长的 20% | needs_review |
| `eef_position_step_too_large` | 末端执行器位置步长超过阈值 | needs_review |
| `joint_columns_not_detected` | 未检测到关节列 | pass/info |

机器人专用夹爪限位可在此处覆盖：

```json
phase5_robot_state.robots
```

当前中心配置包括：

```json
aloha gripper range: 0.0 to 0.1 m
arx5 gripper range: 0.0 to 0.082 m
```

## 静止裁剪规划器

文件：

```text
QA_Pipeline/scripts/plan_standstill_trim.py
```

这不是主阶段运行器的一部分。它是一个独立的、仅生成报告的规划工具，用于检测 episode 开头或结尾不需要的静止片段。

运行：

```bash
python3 QA_Pipeline/scripts/plan_standstill_trim.py \
  --roots Test_Data \
  --output-dir outputs/standstill_trim_test \
  --workers 8 \
  --progress
```

输出：

```text
standstill_trim_plan.csv
standstill_trim_plan.jsonl
standstill_trim_summary.md
```

规划器决策：

| Decision | 含义 |
| --- | --- |
| `no_trim` | 未发现符合条件的开头/结尾静止片段 |
| `trim_candidate` | 看起来安全的边缘裁剪候选 |
| `needs_review` | 裁剪候选会移除过多时长 |
| `reject_too_short_after_trim` | 裁剪后剩余 episode 会过短 |
| `missing_motion_source` | 未找到配置的运动来源 |
| `invalid_timestamps` | 时间戳来源无效 |

规划器不会裁剪视频或重写 CSV。实际同步裁剪仍需要单独的物化步骤。

## 推荐的服务器/NAS 工作流

1. 在服务器上以只读方式挂载 NAS。
2. 从本地仓库根目录将当前仓库部署到服务器：

```bash
rsync -av \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

3. 执行一次 dry-run 发现：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --dry-run
```

4. 运行小规模 smoke test：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --phases 1,2,3 \
  --max-episodes 10 \
  --workers 8 \
  --force-rerun \
  --run-id server-smoke-001
```

5. 打开 dashboard：

```bash
cd outputs/server_smoke
python3 -m http.server 8080
```

然后浏览：

```text
http://<server-ip>:8080/dashboard.html
```

6. 在运行更大规模测试前，先复核 `fail` 和 `needs_review` episode。
7. 只有在理解 smoke test 结果后，再运行更大批次。

## 实用命令参考

Dry-run 发现：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py --roots Test_Data --dry-run
```

在 10 个 episode 上运行第 1 到第 3 阶段：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/test/qa.db \
  --output-dir outputs/test \
  --phases 1,2,3 \
  --max-episodes 10 \
  --force-rerun \
  --run-id test-001
```

使用 8 个 workers 运行当前所有阶段：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Data \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --phases 1,2,3,4,5 \
  --workers 8 \
  --force-rerun \
  --run-id full-001
```

从已有数据库手动生成 dashboard：

```bash
python3 QA_Pipeline/scripts/generate_dashboard.py \
  --db-path outputs/full/qa.db \
  --output outputs/full/dashboard.html
```

## 安全规则

- 先在复制出的样本或只读 NAS 挂载上运行。
- smoke test 使用 `--max-episodes`。
- 在任何清理或隔离步骤前，先复核 `dashboard.html`、`quality_report.csv` 和 `quality_findings.jsonl`。
- 在本地验证并经过复核前，不要对 NAS 源数据执行裁剪或隔离操作。
- 输出目录应与源 episode 文件夹分开。
- 仅在明确想重新计算所选阶段时使用 `--force-rerun`。

## 当前限制

- 主 QA Pipeline 只报告分类，不移动或删除数据。
- 静止裁剪规划器只报告裁剪候选，目前还不会裁剪视频或 CSV。
- 第 4 阶段需要 OpenCV；没有 OpenCV 时，流水线会在写入 episode QA 结果前退出。
- `--workers` 当前会加速第 4 阶段、第 5 阶段，以及独立的静止裁剪规划器。更早阶段基本仍为顺序执行。
- 部分阈值基于当前样本数据校准，在 NAS 全量规模强制执行前应再次复核。
