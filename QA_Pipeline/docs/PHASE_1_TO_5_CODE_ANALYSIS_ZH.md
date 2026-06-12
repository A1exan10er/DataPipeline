# QA Pipeline Phase 1-5 源码分析说明

分析日期：2026-06-11

分析范围：

- `QA_Pipeline/scripts/run_pipeline.py`
- `QA_Pipeline/scripts/pipeline/qa_core.py`
- `QA_Pipeline/scripts/pipeline/qa_config.py`
- `QA_Pipeline/scripts/pipeline/phase1_metadata.py`
- `QA_Pipeline/scripts/pipeline/phase2_duration.py`
- `QA_Pipeline/scripts/pipeline/phase3_timestamp.py`
- `QA_Pipeline/scripts/pipeline/phase4_video.py`
- `QA_Pipeline/scripts/pipeline/phase5_robot_state.py`
- `QA_Pipeline/configs/quality_rules.json`

本文只描述当前源码实际行为。仓库内已有部分英文/中文说明文档，但有些内容已经落后于代码，例如 `--workers` 的覆盖范围、Phase 3 的丢帧阈值、Phase 5 支持的机器人配置等；本文以源码为准。

## 1. 总体结论

这套代码实现的是一个数据质量检查流水线。它从一个或多个 root 目录递归发现 `episode_*` 文件夹，为每个 episode 建立或读取 SQLite 状态记录，然后按 Phase 1 到 Phase 5 运行检查。检查结果以两类对象保存：

- `EpisodeState`：一个 episode 的汇总状态，包括路径、任务、日期、操作员、机器人、已完成阶段、阶段状态、指标、最终状态等。
- `Finding`：某一项具体检查发现，包括 episode 路径、phase 编号、检查名、严重程度、状态、说明文本和细节字典。

流水线本身不会移动、删除、裁剪或改写源数据。它主要读取 `metadata.json`、CSV、视频文件和文件系统结构，然后把结果写到 SQLite 数据库和输出报告中。

默认执行所有已注册阶段：1、2、3、4、5。也可以通过 `--phases` 只运行指定阶段。所有阶段结束后，`run_pipeline.py` 会导出：

- `quality_report.csv`：每个 episode 一行的汇总表。
- `quality_findings.jsonl`：每条 finding 一行的详细 JSONL。
- `quality_summary.md`：按状态、任务、操作员、机器人、问题类型汇总的 Markdown。
- `dashboard.html`：HTML dashboard。

## 2. 执行控制和状态判定

### 2.1 Episode 发现

`qa_core.discover_episodes()` 从传入 root 开始递归遍历目录。规则是：

- 目录名以 `episode_` 开头，就认为它是一个 episode。
- 找到 episode 后不再继续深入该 episode 子目录。
- 跳过名为 `_quarantine` 的目录树。

这意味着只要目录名以 `episode_` 开头，即使内部结构不完整，也会进入 Phase 1，由 Phase 1 给出失败原因。

### 2.2 上下文推断

新 episode 的 `task/date/operator/robot/controller` 由 `qa_core.infer_context()` 推断：

- `task` 优先来自 `metadata.task_key`，否则来自路径中日期目录前一级。
- `date` 是路径中第一个 8 位数字目录。
- `operator` 是日期目录后一级。
- `robot` 来自 `metadata.robot`。
- `controller` 来自 `metadata.controller` 或 `metadata.controller_type`，再 fallback 到 episode 文件夹名末尾字段。

如果 root 直接传到日期目录或更深层目录，路径推断可能仍能找到日期和 operator，但 task 推断更依赖 `metadata.task_key`。

### 2.3 失败跳过策略

默认情况下，`run_pipeline.py` 在每个阶段前会过滤可运行 episode：

- 如果某个 episode 在更早阶段已经是 `fail`，后续阶段不会再运行。
- `warning` 和 `needs_review` 不会阻止后续阶段。
- 使用 `--continue-after-fail` 可以让后续阶段继续运行，从而获得更完整诊断。

这个策略能节省视频解码和大 CSV 读取成本，但代价是：失败 episode 的后续潜在问题不会被收集。

### 2.4 状态聚合规则

`qa_core.decide_status()` 根据一个阶段的 findings 聚合阶段状态：

- 任何 `critical` finding -> `fail`。
- 任何 `major` 且 status 为 `fail` -> `fail`。
- 任何 status 为 `needs_review` -> `needs_review`。
- 任何 `major` -> `warning`。
- 任何 `minor` -> `warning`。
- 没有问题 -> `pass`。

最终状态由 `_final_status()` 聚合所有阶段状态：

- 任一阶段 `fail` -> 最终 `fail`。
- 否则任一阶段 `needs_review` -> 最终 `needs_review`。
- 否则任一阶段 `warning` -> 最终 `warning`。
- 否则 `pass`。

### 2.5 并行执行现状

当前 `run_pipeline.py` 会把 `--workers` 传给 Phase 1-5，且 Phase 1、2、3、4、5 的源码都实现了 multiprocessing 分支。

需要注意：`run_pipeline.py` 的 argparse help 仍写着 `--workers` “currently Phases 4 and 5”，部分旧文档也写 Phase 1-3 基本顺序执行。这已经不准确。当前代码事实是 Phase 1-5 都支持 `workers > 1` 的并行路径，但 Phase 2 和 Phase 3 仍需在 per-episode 并行检查后回到主进程执行组级检查。

## 3. Phase 1：结构与元数据检查

源码文件：`phase1_metadata.py`

### 3.1 目的

Phase 1 判断一个 episode 是否具备后续检查和训练所需的基础结构：文件夹名、`metadata.json`、必需元数据字段、模态目录、必需文件和质量标签。

### 3.2 输入

主要读取：

- episode 文件夹路径。
- `metadata.json`。
- `.checksum_manifest` 是否存在。
- 各模态目录及其中的 `video.mp4`、`timestamps.csv`、`data.csv`。

### 3.3 具体检查

Phase 1 对未完成 Phase 1 的 episode 依次执行：

1. `episode_folder_name`
   检查 episode 文件夹名是否以 `episode_` 开头。否则 `major/fail`。

2. `metadata_exists` / `metadata_valid_json`
   通过 `qa_core.load_metadata()` 读取 `metadata.json`。缺失、不可读、JSON 无效或顶层不是对象，都会生成 Phase 1 的 `critical/fail` finding。

3. `parent_path_structure`
   检查路径尾部是否像 `<task>/<date>/<operator>/<episode>`。实现方式是看 `episode_path.parts[-3]` 是否为 8 位数字日期，并要求 `parts[-2]` 非空。不满足则 `minor/warning`。

4. `required_metadata_field`
   检查下列字段：
   - `task_key`
   - `episode_index`
   - `duration_seconds`，必须为正数。
   - `total_frames`，必须为正数。
   - `modalities`，必须是非空 dict。
   - `fps_actual` 或 `fps_config` 至少一个为正数。
   - `quality` 字段必须存在。

5. `modality_folder_missing`
   对 `metadata.modalities` 中列出的每个模态检查对应目录是否存在。
   特殊处理：如果元数据键是 `actions`，实际目录可以是 `action.*` 或 `actions.*`。

6. `checksum_manifest_missing`
   检查 `.checksum_manifest` 是否存在。缺失为 `minor/warning`，不会 fail。

7. `required_modality_file_missing` / `required_modality_file_empty`
   对每个模态目录检查必需文件：
   - 普通图像模态 `observation.image.*`：需要 `video.mp4` 和 `timestamps.csv`。
   - 光流图像模态 `observation.image.flow_*`：只需要 `video.mp4`。
   - `actions`、`action.*`、`actions.*`、`observation.state.*`：需要 `data.csv`。

8. `quality_labels_missing`
   检查 `metadata.quality.labels` 是否为非空 list。否则 `minor/warning`。

### 3.4 指标记录

Phase 1 会写入：

- `p1_modality_count`
- `p1_image_modality_count`
- `p1_has_checksum_manifest`
- `p1_quality_labels`

### 3.5 潜在问题和改进建议

1. 父路径结构检查较弱。
   当前只看倒数第 3 级是否是 8 位数字、倒数第 2 级是否非空，并不验证 `<task>/<date>/<operator>/<episode>` 的完整语义。建议使用 `infer_context()` 的相对路径结果统一校验。

2. `quality` 只检查存在，`quality.labels` 才检查非空。
   如果 `quality` 是字符串或其他非 dict 类型，`required_metadata_field` 会通过，但 `quality_labels_missing` 会 warning。若训练系统强依赖 dict 结构，建议把 `quality` 类型错误提升为更明确的 finding。

3. `.checksum_manifest` 只检查存在，不校验内容。
   如果目标是数据完整性验证，后续应增加 checksum 文件格式和实际校验。

4. 对额外未知模态基本不报错。
   `_modality_names_to_check()` 会把看起来像 action/state/image 的实际目录也纳入必需文件检查，但未知命名模态不会被验证。建议对未知但包含数据文件的目录输出 info 或 warning，便于发现命名漂移。

## 4. Phase 2：时长、帧数和行数一致性

源码文件：`phase2_duration.py`

### 4.1 目的

Phase 2 主要判断 episode 的时长、总帧数、FPS、时间戳行数、状态 CSV 行数、各模态数量是否互相一致，并用同任务分组统计发现时长异常 episode。

### 4.2 输入

主要读取：

- `metadata.duration_seconds`
- `metadata.total_frames`
- `metadata.fps_actual` 或 `metadata.fps_config`
- `metadata.modalities`
- 图像模态 `timestamps.csv`
- 状态模态 `data.csv`

### 4.3 单 episode 检查

1. `duration_under_5s`
   如果 `duration_seconds` 是正数但小于 5 秒，直接 `critical/fail`。这是硬阈值，不依赖任务中位数。

2. `duration_not_positive`
   `duration_seconds` 不是正数时 `critical/fail`。

3. `total_frames_not_positive`
   `total_frames` 不能转为正整数时 `critical/fail`。

4. `duration_frames_fps_inconsistent`
   计算 `expected_frames = duration_seconds * fps`，其中 fps 优先用 `fps_actual`，否则用 `fps_config`。如果 `abs(total_frames - expected_frames) / expected_frames > 0.10`，则 `major/fail`。

5. `timestamps_row_count_mismatch`
   对每个普通图像模态，统计 `timestamps.csv` 数据行数。如果与 `metadata.total_frames` 的误差比例大于 10%，则 `major/fail`。

6. `timestamps_unreadable`
   如果图像模态 `timestamps.csv` 无法读取，则 `major/fail`。

7. `state_csv_row_count_mismatch`
   对每个 `observation.state.*` 模态，统计 `data.csv` 行数。如果与 `duration_seconds * fps` 的误差比例大于 15%，则 `minor/warning`。

8. `modality_frame_count_misaligned`
   从 `metadata.modalities` 读取每个模态的 `frames` 或 `rows` 计数，比较最大值和最小值：
   - spread <= 3：通过。
   - 4 <= spread <= 10：`minor/warning`。
   - spread > 10：`major/needs_review`。

对触觉 state 模态有特殊 fallback：如果 `observation.state.*tactile*` 的 rows 为 0，会尝试用对应 `observation.image.*tactile*` 在 `metadata.frame_integrity` 中的 `frame_count`。

### 4.4 组级检查

Phase 2 在所有 pending episode 的单体检查完成后执行组级检查：

1. `duration_task_outlier`
   按 task 分组。每组至少 5 个 episode 时，计算 duration 中位数和 IQR。若 `abs(duration - median) / IQR > 3.0`，标记 `minor/needs_review`。

2. `duration_absolute_too_short`
   按 task 分组。每组至少 3 个 episode 时：
   - `duration < median * 0.20`：`major/fail`。
   - `duration < median * 0.40`：`minor/needs_review`。

3. `duration_absolute_too_long`
   同一组内：
   - `duration > median * 2.50`：`minor/needs_review`。

### 4.5 指标记录

Phase 2 会写入：

- `p2_duration_seconds`
- `p2_total_frames`
- `p2_fps`
- `p2_expected_frames`
- `p2_frame_count_error_ratio`
- `p2_timestamps_checked`
- `p2_timestamps_mismatch_count`
- `p2_duration_iqr_distance`
- `p2_modality_frame_spread`
- `p2_modality_min_frames`

### 4.6 潜在问题和改进建议

1. 配置项未完全使用。
   `quality_rules.json` 中有 `phase2_duration.length_alignment.max_video_action_difference = 3`，但源码里模态 spread 阈值硬编码为 3 和 10。建议改为配置驱动。

2. CSV 行数统计会完整扫描文件。
   `count_csv_rows()` 会逐行遍历 CSV。对 NAS 上大量 episode 成本较高。可考虑从 metadata 中可信字段快速筛选，再对异常或抽样数据做文件级确认。

3. 状态 CSV 行数使用全局 FPS 推断，可能不适合低频 state。
   当前 state `data.csv` 预期行数是 `duration_seconds * fps`，而 fps 来自 episode 图像 FPS。若某些 state 模态本来低频，这会误报。建议优先读取 `metadata.modalities[modality].hz/frequency/hz_nominal`。

4. task 分组依赖 `state.task` 或 `metadata.task_key`。
   如果 root 选择导致 task 推断为空，且 metadata 缺少 `task_key`，不同任务可能被合并到空 task 组，组级离群判断会失真。

5. `total_frames` 的整数转换较宽松。
   `_positive_int()` 会把 `12.9` 转为 `12`。如果 metadata 应该严格为整数，建议对非整数字符串或浮点残留单独 warning。

## 5. Phase 3：图像时间戳、频率和同步检查

源码文件：`phase3_timestamp.py`

### 5.1 目的

Phase 3 检查普通图像模态的时间戳质量，包括时间戳是否递增、是否重复、是否存在丢帧、实际 FPS 是否偏离预期、多相机起止时间是否同步，以及同任务同机器人组内的频率/连续丢帧离群。

注意：当前 Phase 3 只检查 `observation.image.*` 且排除 `observation.image.flow_*`。源码注释明确说 state/action 时间戳由 Phase 5 检查。文件中存在 `_check_large_gaps()` 等数据模态逻辑，但当前 `_timestamp_modalities()` 不会返回 state/action 模态，因此这部分对当前执行路径基本不可达。

### 5.2 输入

主要读取：

- 图像模态 `timestamps.csv`
- 可选 `timestamps_raw.csv`
- `metadata.modalities`
- `metadata.frame_integrity`
- episode 级 `fps_actual` 或 `fps_config`
- 模态级 `hz`、`frequency`、`hz_nominal`

### 5.3 时间戳读取方式

对图像模态，Phase 3 使用 `_read_image_timestamps()`：

- 读取 CSV 中 `timestamp_ms` 可解析的行。
- 只保留 `is_new == "1"` 的 timestamp。

这意味着它关注“新图像帧”的时间戳，而不是所有 timestamp 行。

### 5.4 单 episode 检查

1. `timestamps_unreadable`
   如果 timestamp 源文件不存在或无法读取，生成 `major/fail`。

2. `timestamps_not_monotonic`
   检查 timestamp 是否严格递增。违规比例：
   - >= 5%：`major/fail`
   - >= 1%：`major/needs_review`
   - < 1%：`minor/warning`

3. `duplicate_timestamps`
   检查重复 timestamp。重复比例：
   - >= 5%：`major/fail`
   - >= 1%：`major/needs_review`
   - < 1%：`minor/warning`

4. `frame_drop_ratio_high`
   从 `metadata.frame_integrity[modality]` 读取 `frame_count` 和 `total_drops`，计算 `drop_ratio = total_drops / frame_count`。
   阈值来自配置：
   - 普通图像：默认 `0.15`
   - 触觉图像：默认 `0.20`
   超过阈值为 `major/fail`。

5. `consecutive_frame_drops_high`
   从 `metadata.frame_integrity[modality].max_consecutive_drops` 读取最大连续丢帧数。默认阈值为 `25`，达到或超过即 `major/fail`。

6. `abnormal_fps_loss`
   实际 FPS 计算为 `(len(timestamps) - 1) / ((last - first) / 1000)`。
   预期 FPS 优先使用模态元数据中的 `hz/frequency/hz_nominal`，否则用 episode 的 `fps_actual/fps_config`。
   如果 FPS 低于预期超过默认 10%，则 `major/fail`。

7. `abnormal_fps_gain`
   如果实际 FPS 高于预期超过默认 10%，则 `minor/warning`。

8. `timestamps_raw_count_mismatch`
   如果同一模态同时存在 `timestamps.csv` 和 `timestamps_raw.csv`，统计两者行数。差值大于 2 行则 `minor/warning`。

9. `modality_start_alignment_spread` / `modality_end_alignment_spread`
   对所有可读图像模态，比较第一帧 timestamp 和最后一帧 timestamp。起点或终点最大差值超过 `500 ms` 就 `major/needs_review`。

### 5.5 组级检查

1. `actual_fps_group_outlier`
   按 `task + "_" + robot` 分组，并在每个模态内计算 actual FPS 的 median/IQR。每组每模态至少 5 个值才启用。若某 episode 的距离超过 `3 IQR`，则 `minor/needs_review`。

2. `consecutive_drops_outlier`
   同样按 task+robot 和模态分组。
   - 组大小至少 5：使用 `median + 3 * IQR`，超过则 `major/needs_review`。
   - 组大小小于 5：使用 fallback 阈值，默认 `max_consecutive_warn = 10`，达到或超过则 `minor/warning`。

### 5.6 指标记录

每个图像模态记录：

- `p3_<modality>_actual_fps`
- `p3_<modality>_row_count`
- `p3_<modality>_max_gap_ms`
- `p3_<modality>_duplicate_count`
- `p3_<modality>_monotonic_ok`

从 `frame_integrity` 记录：

- `p3_<modality>_total_drops`
- `p3_<modality>_max_consecutive_drops`
- `p3_<modality>_drop_ratio`

### 5.7 潜在问题和改进建议

1. `is_new` 缺失时可能静默通过。
   `_read_image_timestamps()` 只保留 `is_new == "1"`。如果 CSV 没有 `is_new` 列，或值不是字符串 `"1"`，结果会是空列表，但这不是 `None`，因此不会触发 `timestamps_unreadable`。后续很多检查会因空列表跳过，可能产生误判。建议对图像 timestamp 行数为 0 单独报 `major/fail` 或至少 warning。

2. 图像 timestamp 的大间隔检查未实际运行。
   `_check_large_gaps()` 只在非图像模态分支调用，而当前 Phase 3 不返回非图像模态。因此图像流即使存在大时间间隔，也主要依赖 actual FPS 和 frame_integrity 间接发现。建议为图像 timestamp 也加入 interval gap 检查。

3. alignment 阈值硬编码。
   `ALIGNMENT_THRESHOLD_MS = 500.0` 写死在源码中。建议加入 `quality_rules.json`，方便不同采集系统调参。

4. 实际 FPS 使用首尾 timestamp，容易被头尾异常影响。
   如果首帧或尾帧 timestamp 异常，actual FPS 会偏移。建议增加基于 interval median 的辅助指标，或同时报告 mean/median interval。

5. `save_findings()` 在 Phase 3 组级检查中可能重复写入同一 phase。
   当前串行路径会先保存 per-episode findings，再在有组级 finding 的 episode 上用包含组级 finding 的列表替换写入。这通常不会造成数据库重复，因为 `save_findings()` 会先 delete 再 insert，但增加了写库次数。建议 Phase 3 串行路径也改成先收集所有结果，组级检查后统一 `_finish_state()`，与并行路径更一致。

6. `frame_integrity` 完全依赖 metadata。
   如果 metadata 中 `frame_integrity` 缺失或不可信，丢帧检查会跳过。建议可选地从 timestamp 间隔或 raw/processed 行数推断补充丢帧风险。

## 6. Phase 4：视频健康检查

源码文件：`phase4_video.py`

### 6.1 目的

Phase 4 用 OpenCV 检查每个普通图像模态的视频是否能打开、帧数/时长/分辨率是否合理、采样帧是否黑屏/白屏/冻结，以及 ARX5 双腕部视角是否同时静止。

### 6.2 输入

主要读取：

- `observation.image.*/video.mp4`
- `metadata.total_frames`
- `metadata.duration_seconds`
- `metadata.cameras[modality]`
- 模态目录下可选 `config.csv`

只处理普通图像模态，排除 `observation.image.flow_*`。而且只有存在 `video.mp4` 的图像模态才会进入 Phase 4；缺失视频文件主要由 Phase 1 检出。

### 6.3 依赖检查

Phase 4 需要 `opencv-python-headless` 或可导入的 `cv2`。如果导入失败，`validate_dependencies()` 会抛出 `PipelineConfigurationError`，主流程会在写 episode 结果前停止。

### 6.4 视频属性检查

对每个视频：

1. `video_not_openable`
   `cv2.VideoCapture` 无法打开时，`critical/fail`。

2. `video_frame_count_unreadable`
   OpenCV 读取到的 frame count <= 0 时，`major/fail`。

3. `video_frame_count_mismatch`
   如果 metadata 有正数 `total_frames`，并且视频帧数与 metadata 误差超过 10%，则 `major/fail`。

4. `video_duration_mismatch`
   如果 frame count、fps、metadata duration 都有效，计算 `video_duration = frame_count / fps`。与 metadata duration 误差超过 10%，则 `minor/warning`。

5. `video_resolution_mismatch`
   预期分辨率优先来自 `metadata.cameras[modality]`，其次来自 `modality/config.csv`。支持字段：
   - `width` / `height`
   - `actual_width` / `actual_height`
   - `configured_width` / `configured_height`

   如果实际宽高与预期不一致，则 warning。但有一个例外：实际宽度等于预期宽度，实际高度大于等于预期高度时，认为可能是 letterbox/padding，允许通过。

### 6.5 采样帧检查

采样位置固定为：

`0%, 15%, 30%, 45%, 60%, 75%, 90%, 100%`

最多采样 8 帧；如果视频帧数 <= 8，则采样每一帧。

每帧转灰度后计算平均亮度：

- 亮度 < 5：黑帧。
- 亮度 > 250：白帧。

对相邻采样帧计算灰度平均绝对差：

- 如果所有相邻采样差值都 < 1.0，则认为视频冻结，生成 `video_frozen`，`major/fail`。

黑帧/白帧 finding：

- 只要有黑帧或白帧，至少 `major/needs_review`。
- 如果黑帧和白帧合计超过采样数一半，则 `critical/fail`。

### 6.6 ARX5 双腕部静止检查

只对 `state.robot.lower() == "arx5"` 执行。

检查两个模态：

- `observation.image.left_wrist_view`
- `observation.image.right_wrist_view`

如果两个模态都存在采样帧，并且每个相机超过 80% 的相邻采样帧差值都小于 5.0，则认为两个腕部视角同时静止，生成 `both_wrist_views_still`，`major/fail`。

### 6.7 指标记录

每个视频记录：

- `p4_<modality>_openable`
- `p4_<modality>_video_frames`
- `p4_<modality>_video_fps`
- `p4_<modality>_video_duration_s`
- `p4_<modality>_width`
- `p4_<modality>_height`
- `p4_<modality>_mean_brightness`
- `p4_<modality>_min_frame_diff`

### 6.8 潜在问题和改进建议

1. Phase 4 重复读取 metadata。
   `_metadata_total_frames()`、`_metadata_duration()`、`_expected_resolution()` 都会通过 `_episode_metadata()` 重新读取 `metadata.json`。一个视频可能触发多次 metadata 读取。建议把已加载 metadata 从 `EpisodeState` 传入 helper，降低 NAS 小文件读取成本。

2. 并行 worker 的 `EpisodeState` 信息较少。
   Phase 4 并行 worker 只接收 `episode_path` 和 `robot`。目前检查主要足够，但如果后续增加依赖 task/operator/controller 的逻辑，需要同步传入完整上下文。

3. 冻结检测只基于最多 8 个采样帧。
   如果视频中只有短时间冻结，或采样点刚好错过异常，可能漏检。建议增加可配置采样数量，或对关键相机使用连续窗口抽样。

4. 黑/白帧阈值固定。
   亮度 < 5、> 250 对正常 RGB 视频合理，但对特殊传感器或触觉图像可能过严或过松。建议按 camera type 配置。

5. letterbox 允许规则较宽。
   只要宽度匹配且高度更大就通过，没有验证 padding 是否真的是黑边，也没有检查纵横比。建议在 warning details 中记录该容忍路径，或抽样检测上下边缘是否确为 padding。

6. 只检查 ARX5 双腕部同时静止。
   其他机器人或其他关键视角没有类似 motion check。建议把关键相机组合和阈值配置化。

## 7. Phase 5：机器人状态合理性检查

源码文件：`phase5_robot_state.py`

### 7.1 目的

Phase 5 检查机器人状态和动作 CSV 中的数值是否物理合理，包括关节/夹爪限位、逐帧跳变、速度、加速度、抖动、长时间静止和末端执行器位置跳变。

### 7.2 支持的机器人配置

当前 `ROBOT_CONFIGS` 中有：

- `arx5`
- `flexiv`
- `aloha`

未知机器人会回退到 `arx5` 默认配置，并生成 `robot_config_fallback`，但该 finding 是 `info/pass`，不会阻塞。

配置包括：

- 单臂关节数。
- 关节限位。
- 夹爪限位。
- 最大关节速度。
- 最大夹爪速度。
- 最大关节逐帧 step。
- 最大夹爪逐帧 step。
- 最大 EEF 位置 step。
- 最大 EEF 旋转 step。
- jitter 平滑窗口和阈值。
- 静止判定阈值。
- 最大加速度。

其中 ARX5 的部分阈值注释中说明来自 20260602-20260603 的 3 个任务校准数据；Flexiv/Aloha 多数阈值是保守默认。

### 7.3 输入模态

关节位置：

- `actions.joint_position/data.csv`
- `observation.state.joint_position/data.csv`

关节速度：

- `observation.state.joint_velocity/data.csv`

末端位姿：

- `actions.eef_pose/data.csv`
- `action.eef_pose/data.csv`
- `observation.state.eef_pose/data.csv`

如果没有实测速度文件，Phase 5 会从关节位置和 timestamp 估计速度与加速度。

### 7.4 CSV 读取和列识别

CSV 通过 `csv.DictReader` 读取，所有字段尝试转成 float，失败则为 `None`。列识别规则：

ARX 关节列：

- `left_j*`
- `right_j*`

ARX 速度列：

- `left_v*`
- `right_v*`

Flexiv 风格关节列：

- `j1`、`j2` 等。
- `joint_*.pos`

Flexiv 风格速度列：

- `v1`、`v2` 等。
- `joint_*.vel`

夹爪列：

- 列名包含 `gripper`，大小写不敏感。

EEF 位置列：

- 双臂：`left_x/left_y/left_z`，`right_x/right_y/right_z`
- TCP：`tcp.x/tcp.y/tcp.z`

### 7.5 关节位置检查

对 `actions.joint_position` 和 `observation.state.joint_position`：

1. `joint_columns_not_detected`
   如果没有识别出关节列，生成 `info/pass`。

2. `joint_nan_inf`
   对识别出的关节列和夹爪列检查 NaN、Inf 或不可解析值。发现后 `critical/fail`。

3. `timestamps_missing_or_unparseable`
   如果 `timestamp_ms` 缺失或没有可解析值，`major/fail`。

4. `timestamps_not_monotonic`
   timestamp 非严格递增，按违规比例判定：
   - >= 5%：`major/fail`
   - >= 1%：`major/needs_review`
   - < 1%：`minor/warning`

5. `joint_out_of_limits`
   关节值超出机器人配置的 `joint_limits_rad` 加容差范围，则 `minor/needs_review`。

6. `gripper_out_of_limits`
   夹爪值超出 `gripper_limits_m` 加容差范围，则 `minor/needs_review`。

7. `gripper_mean_too_low_remap_needed`
   如果某个夹爪列平均值低于配置阈值，默认 `0.005 m`，则 `minor/needs_review`，并在 details 中给出 `action: remap_gripper_distance`。

8. `joint_step_too_large`
   相邻关节值差的绝对值超过 `max_joint_step_rad`，则 `minor/needs_review`。

9. `gripper_step_too_large`
   相邻夹爪值差超过 `max_gripper_step_m`，则 `minor/needs_review`。

10. `jitter_high`
   对关节值做 centered moving average，计算平均绝对残差作为 jitter score：
   - >= `jitter_score_fail`：`major/fail`
   - >= `jitter_score_warn`：`minor/warning`

### 7.6 静止段检查

静止检查优先使用 `observation.state.joint_position`，如果没有则使用 `actions.joint_position`。

逻辑：

- 排除 `timestamp_ms`、`is_standstill` 和包含 `gripper` 的列。
- 对相邻两行，如果所有关节变化都小于 `static_motion_threshold_rad`，且时间递增，则认为该 pair 静止。
- 连续静止段超过 `STANDSTILL_BUFFER_MS = 5000` 才记录。
- 每个静止段生成 `operator_standstill`，`minor/warning`。
- 只统计超过 5 秒缓冲后的 excess 时间。
- 如果总 excess 时间超过 episode 时长 20%，生成 `operator_standstill_excessive`，`major/needs_review`。

### 7.7 速度和加速度检查

如果存在 `observation.state.joint_velocity/data.csv`：

- 读取实测速度列。
- 检查 NaN/Inf。
- 检查 timestamp 单调。
- 对每列绝对速度取 p99，如果超过 `max_joint_velocity_rad_s`，生成 `joint_velocity_exceeded`，`minor/needs_review`。
- 从速度和 timestamp 计算加速度，p99 超过 `max_acceleration_rad_s2` 时生成 `joint_acceleration_high`，`minor/warning`。

如果不存在实测速度：

- 对每个关节位置模态，用相邻位置差除以 timestamp delta 估计速度。
- 对估计速度执行同样 p99 阈值检查。
- 从位置二阶差估计加速度并检查 p99。

### 7.8 EEF 位姿检查

对 `actions.eef_pose`、`action.eef_pose`、`observation.state.eef_pose`：

- 读取 EEF 位置列。
- 计算相邻点三维欧氏距离。
- 如果超过 `max_eef_position_step_m`，生成 `eef_position_step_too_large`，`minor/needs_review`。

源码中有 `max_eef_rotation_step_rad` 配置，但当前没有看到对 EEF 旋转 step 的实际检查。

### 7.9 指标记录

Phase 5 会写入：

- `p5_max_joint_abs`
- `p5_max_joint_step`
- `p5_max_velocity`
- `p5_max_acceleration`
- `p5_jitter_score`
- `p5_standstill_segment_count`
- `p5_standstill_total_excess_ms`
- `p5_standstill_excess_ratio`
- `p5_nan_inf_count`
- `p5_joint_limit_violations`

### 7.10 潜在问题和改进建议

1. 关节列未识别时仍是 `info/pass`。
   如果某个 episode 的关节 CSV 存在但列命名不符合规则，Phase 5 会记录 `joint_columns_not_detected`，但 status 是 pass。这可能让实际未检查的数据通过。建议至少改为 `minor/warning`，或在关键模态中升为 `major/needs_review`。

2. `robot` 参数没有真正影响列识别。
   `_detect_columns(headers, robot)` 接收 robot，但内部只是同时尝试 ARX 和 Flexiv 风格列，没有按 robot 类型选择，也没有 Aloha 专属列规则。建议按 robot config 明确列 schema。

3. 未知机器人 fallback 是 `info/pass`。
   使用 ARX5 阈值检查未知机器人可能产生误报或漏报，但当前不会影响状态。建议未知机器人至少 `minor/warning`，并要求补充配置。

4. 夹爪速度配置没有实际用于夹爪速度检查。
   `max_gripper_velocity_m_s` 存在于配置中，但当前速度检查只针对 joint velocity columns，估计速度也只用 joint columns。建议补充夹爪速度检查或删除未使用配置。

5. EEF 旋转 step 配置未使用。
   `max_eef_rotation_step_rad` 存在但未实现旋转检查。建议实现 quaternion/euler 旋转差检查，或在配置注释中标记为保留项。

6. 过滤 finite values 可能造成时间和值错位。
   `_finite_column_values()` 会分别过滤 timestamp 和某个数据列。如果某些数据列中间有缺失，values 与 timestamps 的索引可能不再一一对应，估计速度/加速度会偏差。建议按行同时过滤 `(timestamp, value)` pair。

7. EEF step 也存在类似错位风险。
   `_eef_steps()` 分别过滤 x/y/z 后再 zip，若某一列缺失值位置不同，三维点可能拼错。建议按行过滤完整 xyz。

8. 加速度 finding 没有写入 threshold。
   `joint_acceleration_high` details 只有 `p99_accel`，没有阈值，不利于报告解释。建议补充 threshold 和 estimated/measured 标记。

9. `LIMIT_TOLERANCE` 注释与值不一致。
   常量为 `0.003`，注释写“1mm / ~0.001 rad tolerance”。建议修正注释或拆分关节/夹爪不同 tolerance。

10. 静止检测对“正常任务内等待”可能误报。
    当前只看关节变化量和 5 秒缓冲，不结合任务语义、相机运动、夹爪动作或 `is_standstill` 标注。建议允许任务级白名单或使用多信号融合。

## 8. 跨阶段问题和总体改进建议

### 8.1 文档与 CLI 帮助需要同步

当前代码中 Phase 1-5 都支持 `workers > 1`，但 CLI help 和部分文档仍说只支持 Phase 4/5。建议更新：

- `run_pipeline.py` 的 `--workers` help。
- `QA_PIPELINE_USER_GUIDE_ZH.md`
- `QA_Pipeline/docs/PIPELINE_TECHNICAL_SUMMARY*.md`

### 8.2 配置化不足

仍有多处硬编码阈值：

- Phase 2：模态 frame spread 阈值 3/10、时长比例 20%/40%/250%。
- Phase 3：alignment 500 ms。
- Phase 4：采样位置、黑白帧亮度阈值、冻结 diff 阈值、ARX wrist still 阈值。
- Phase 5：standstill 5 秒 buffer、20% excessive ratio、limit tolerance。

建议统一迁移到 `quality_rules.json`，并在 reports 中输出使用的阈值版本。

### 8.3 metadata 重复读取

多个阶段会在已经有 `EpisodeState.metadata` 的情况下再次读取 `metadata.json`，Phase 4 尤其明显。大规模 NAS 运行时，小文件随机读取可能成为瓶颈。建议：

- helper 函数优先接收 metadata 参数。
- 并行 worker 参数中传入必要 metadata，而不是只传路径。
- 对 metadata load 失败统一缓存 finding，避免反复尝试。

### 8.4 SQLite 写入频率较高

每个 phase、每个 episode 都会保存 episode state 和 findings。Phase 2 会在组级检查完成后统一写入；Phase 3 串行路径会先写入单 episode 结果，再对有组级 finding 的 episode 替换写入该 phase 的 findings。大规模运行时，SQLite 连接和写入次数较多。建议：

- 每个 phase 批量写入。
- 或引入单 writer 进程/线程。
- 或在组级检查后统一提交。

### 8.5 缺少自动化测试

本次检查没有发现 `QA_Pipeline` 下的测试文件。建议至少增加：

- Phase 1 metadata 缺失、字段缺失、actions 特殊目录匹配测试。
- Phase 2 时长/FPS/行数边界测试。
- Phase 3 `is_new` 缺失、重复 timestamp、alignment、frame_integrity 阈值测试。
- Phase 4 使用小型合成视频测试 openable、black/white/frozen。
- Phase 5 使用小型 CSV 测试列识别、NaN、限位、速度估计、standstill。

### 8.6 严重程度策略需要业务确认

一些检查当前比较保守：

- Phase 5 关节超限是 `minor/needs_review`，不是 fail。
- 未知机器人配置 fallback 是 `info/pass`。
- 关节列无法识别是 `info/pass`。

如果目标是训练数据准入，这些可能过宽。建议根据训练风险重新定义哪些 finding 必须 fail，哪些只是 review。

### 8.7 分组统计应处理小样本和混合任务风险

Phase 2/3 的 IQR 组级检查依赖 task 或 task+robot 分组。如果 task 推断错误或样本太少，结果可能不稳定。建议：

- 报告中记录每个组级 finding 的 group size。
- 当 task 为空时避免跨任务混合，或单独标记 context 缺失。
- 对小样本 fallback 阈值也配置化。

## 9. 建议优先级

高优先级：

1. 修复 Phase 3 `is_new` 缺失导致空 timestamp 静默通过的问题。
2. 将 Phase 5 关节列无法识别从 `info/pass` 调整为 warning 或 needs_review。
3. 更新 `--workers` CLI help 和用户文档，避免运行预期错误。
4. 给 Phase 5 速度/加速度估计改成按行对齐过滤，避免缺失值导致 timestamp/value 错位。

中优先级：

1. Phase 4 避免重复读取 metadata。
2. 把硬编码阈值迁移到 `quality_rules.json`。
3. 为 Phase 3 图像 timestamp 增加 interval gap 检查。
4. 为 Phase 5 补充夹爪速度和 EEF 旋转检查，或删除未使用配置。

低优先级：

1. SQLite 批量写入优化。
2. Phase 4 增强采样策略。
3. checksum manifest 内容校验。
4. 对未知模态和未知机器人输出更明确的 schema drift 报告。

## 10. 总结

Phase 1-5 的整体设计是清晰的：先做结构和 metadata 门禁，再做轻量数量一致性，再做 timestamp，同步之后做视频解码检查，最后做机器人状态合理性检查。它适合在数据进入训练前进行批量 QA，并且所有结果都能落到 SQLite 和报告文件中。

主要风险不在框架结构，而在若干细节：部分阈值硬编码、部分旧文档不准确、Phase 3 对 `is_new` 的假设过强、Phase 5 对列识别失败和未知机器人过于宽松，以及数值 CSV 缺失值过滤可能破坏时间对齐。若要把它作为训练数据准入的可靠门禁，建议优先修复这些会导致漏检或误判的问题，并补上覆盖关键边界条件的单元测试。

## 11. Phase 1 增强计划：基于 checksum manifest 的结构完整性检查

本节是 2026-06-11 根据新增需求补充的 Phase 1 增强计划，只描述建议方案，不代表当前源码已经实现。

### 11.1 样例数据观察

抽样检查 `Test_Folder_For_DataPipeline/Data_Robots` 下的 episode 后，有以下结论：

- 样例中存在大量 `.checksum_manifest`。该文件格式是 JSON object，键是 episode 内部相对路径，值是 64 位 SHA-256 hash。例如键包括 `metadata.json`、`actions.eef_pose/data.csv`、`observation.image.* / video.mp4`、`timestamps.csv` 等。
- 部分 episode 还存在 `checksums.sha256`，格式是传统 `sha256  relative/path` 文本行。但它不是每个 episode 都有；样例中 `.checksum_manifest` 数量明显多于 `checksums.sha256`。
- 需求中提到 md5，但样例文件实际是 SHA-256，不是 MD5。实现时应按 hash 长度或 manifest 格式自动识别，当前样例优先按 SHA-256 处理。
- 样例中存在 `action.eef_pose` 和 `actions.eef_pose` 两种命名，也存在 `actions.joint_position`。
- 样例中存在 `observation.image.flow_*` 目录，并且它们可能出现在 `.checksum_manifest` 中；但它们不一定出现在 `metadata.modalities` 中。

### 11.2 目标行为

Phase 1 增强后应满足：

1. `.checksum_manifest` 是 episode 完整性检查的强依赖。
   如果 `.checksum_manifest` 缺失，Phase 1 应直接生成 `critical/fail` 或 `major/fail` finding，并写明原因。

2. 如果 `.checksum_manifest` 存在，应解析其中的文件清单。
   manifest 中列出的每个相对路径都应在 episode 文件夹中存在，并且应是普通文件、非空文件。缺失或为空应 fail。

3. 如果需要更强校验，应可选计算文件 hash。
   对 manifest 中的每个文件计算 SHA-256，与 manifest 值比对；不一致应 fail。考虑性能，建议提供配置开关：
   - 默认快速模式：只检查 manifest 可读、路径存在、非空。
   - 严格模式：额外计算 hash。

4. `.checksum_manifest` 缺失、无法解析、格式不符合预期、列出文件缺失、列出文件为空、hash 不一致，都必须把原因写到 finding details 中。

5. `observation.image.flow_*` 不是业务必需模态。
   - 如果 metadata 或规则没有要求 flow，则不得因为 flow 目录不存在而 fail。
   - 如果 flow 文件已经被 `.checksum_manifest` 明确列出，那么它就是该 episode 生成时存在的文件；此时缺失代表 episode 与 manifest 不一致，应 fail。这个 fail 原因应表述为 manifest 文件缺失，而不是“缺少必需 flow 模态”。

6. 未知模态只报告，不 fail。
   对 metadata 或文件夹中无法识别的模态，生成 `info/pass` 或 `minor/warning` finding，写入 report 供人工了解 schema drift，但不影响 pass/fail。

7. action 单复数命名要规范化。
   如果实际文件夹名以 `action.` 开头，应在报告中指出建议规范名 `actions.*`。是否自动重命名需要谨慎：重命名会改变源数据和 manifest 路径，必须同步更新 `.checksum_manifest`，否则立即造成 checksum 不一致。建议先实现为 Phase 1 finding，不在 QA pipeline 中自动修改源数据；如确需改名，应做独立迁移工具。

### 11.3 建议检查顺序

建议重构 Phase 1 为以下顺序：

1. 基础路径检查
   检查 episode 目录名和父路径结构。该部分不依赖 metadata 或 manifest。

2. manifest 存在性检查
   检查 `.checksum_manifest` 是否存在。缺失时生成 `checksum_manifest_missing`，状态为 fail。仍可继续读取 metadata 以便报告更多信息，但该 episode 的 Phase 1 状态已经 fail。

3. manifest 解析检查
   新增 `_load_checksum_manifest()`：
   - 读取 JSON。
   - 确认顶层是 dict。
   - 确认 key 是相对路径，不允许绝对路径、不允许 `..` 路径逃逸。
   - 确认 value 是 32 位 MD5 或 64 位 SHA-256 字符串；样例为 64 位 SHA-256。

4. manifest 文件清单检查
   新增 `_check_manifest_files_present()`：
   - 对 manifest 每个路径检查文件存在。
   - 检查是文件而不是目录。
   - 检查大小 > 0。
   - 统计 missing、empty、not_file，分别写入 details。

5. 可选 hash 校验
   新增 `_check_manifest_hashes()`，由配置控制：
   - `phase1_metadata.checksum.verify_hashes: false` 默认关闭。
   - `true` 时根据 hash 长度选择 md5 或 sha256。
   - hash mismatch 生成 `checksum_hash_mismatch`，fail。

6. metadata 读取和字段检查
   保留当前 metadata JSON 合法性和关键字段检查。manifest 不能完全替代 metadata，因为后续 Phase 2-5 需要 `duration_seconds`、`total_frames`、`fps`、`modalities`、`robot` 等语义信息。

7. metadata modalities 与文件夹对齐检查
   简化当前 required file 逻辑：
   - 对 metadata 中的普通图像模态仍要求目录存在，且至少有 `video.mp4` 和 `timestamps.csv`，除非 manifest 已经覆盖且通过。
   - 对 state/action CSV 模态仍要求 `data.csv`，除非 manifest 已经覆盖且通过。
   - 对 `observation.image.flow_*` 从 required modality 检查中排除。
   - 对 unknown modality 只生成 report finding，不 fail。

8. action 命名规范检查
   新增 `_check_action_pluralization()`：
   - 如果发现目录 `action.<suffix>`，报告建议改为 `actions.<suffix>`。
   - 如果同一 episode 中同时存在 `action.<suffix>` 和 `actions.<suffix>`，报告冲突，需要人工处理。
   - 默认不自动重命名，避免破坏 manifest。

### 11.4 新增 finding 建议

建议新增或调整以下 check name：

- `checksum_manifest_missing`
  `.checksum_manifest` 缺失。建议从当前 `minor/warning` 改为 `critical/fail`。

- `checksum_manifest_invalid`
  manifest 不是合法 JSON、不是 dict、hash 值格式不合法，或包含不安全路径。建议 `critical/fail`。

- `checksum_manifest_file_missing`
  manifest 列出的文件缺失。建议 `critical/fail`。details 包含缺失路径列表和数量。

- `checksum_manifest_file_empty`
  manifest 列出的文件为空。建议 `major/fail` 或 `critical/fail`，取决于业务是否认为空文件一定不可用。

- `checksum_hash_mismatch`
  严格模式下 hash 不一致。建议 `critical/fail`。

- `action_modality_singular_name`
  发现 `action.*` 命名。建议 `minor/warning` 或 `info/pass`。如果计划后续统一命名，可设为 warning。

- `unknown_modality_detected`
  metadata 或实际目录中存在未知模态。建议 `info/pass`，details 记录未知名称。

- `flow_modality_ignored`
  可选 finding。发现 `observation.image.flow_*` 时记录其存在但不参与必需模态判断。建议 `info/pass`；如果 report 太吵，可以只写入 metrics。

### 11.5 配置建议

建议在 `quality_rules.json` 增加：

```json
{
  "phase1_metadata": {
    "checksum": {
      "required": true,
      "verify_hashes": false,
      "accepted_algorithms": ["sha256", "md5"],
      "max_missing_paths_in_details": 50
    },
    "modalities": {
      "ignore_flow_modalities": true,
      "unknown_modality_status": "pass",
      "singular_action_status": "warning"
    }
  }
}
```

默认不启用 hash 全量计算，是出于性能考虑：大规模 NAS 上对每个视频文件计算 SHA-256 会非常慢。可以先用 manifest 文件清单做结构完整性门禁，再在抽样或严格运行中启用 hash。

### 11.6 对当前 Phase 1 的简化边界

checksum manifest 可以简化“文件是否存在”的部分，但不能完全替代 Phase 1：

- 可以简化：
  - 对 manifest 列出的文件逐个检查存在/非空。
  - 许多 `video.mp4`、`timestamps.csv`、`data.csv` 的存在性检查可通过 manifest 清单统一完成。

- 不能替代：
  - `metadata.json` 是否是合法 JSON。
  - metadata 必需字段和类型检查。
  - `duration_seconds`、`total_frames`、fps、`modalities` 这些语义字段。
  - 父路径和 episode 命名规范。
  - unknown modality、action 命名规范这类 schema/reporting 逻辑。

建议实现时不要删除 metadata 必需字段检查，只把“按模态推断必需文件”的逻辑降级为 manifest 缺失或 manifest 不完整时的 fallback。

### 11.7 测试计划

新增 Phase 1 单元测试或小型集成测试：

1. `.checksum_manifest` 缺失 -> Phase 1 fail，原因包含 `checksum_manifest_missing`。
2. `.checksum_manifest` 非 JSON -> fail，原因包含解析错误。
3. manifest 包含绝对路径或 `../` -> fail，防止路径逃逸。
4. manifest 列出文件缺失 -> fail，details 包含缺失文件。
5. manifest 列出空文件 -> fail。
6. 严格 hash 模式下 hash 不一致 -> fail。
7. `observation.image.flow_*` 不在 metadata、不存在 -> 不 fail。
8. `observation.image.flow_*` 在 manifest 中但文件缺失 -> 因 manifest 不一致 fail。
9. `action.eef_pose` 存在 -> report `action_modality_singular_name`，默认不自动改名。
10. unknown modality 存在 -> report，但 phase status 不因它变成 fail。
