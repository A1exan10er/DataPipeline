# QA Pipeline 用户指南

最后更新：2026-06-30

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

先进入仓库并激活虚拟环境：

```bash
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

然后从仓库根目录运行流水线：

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

服务器或较大的本地运行可使用 workers，但不要占满机器 CPU。8 核服务器建议从 `--workers 3` 或 `--workers 4` 开始，并让 resource guard 在负载或可用内存不安全时暂停：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/qa_20260611/qa_pipeline.db \
  --output-dir outputs/qa_20260611 \
  --phases 1 \
  --workers 4 \
  --batch-size 5000 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --resource-check-interval 15 \
  --force-rerun
```

`--workers` 会传给所有已注册阶段。第 1 到第 5 阶段有并行执行路径。第 6 阶段会接收该参数，但目前会逐个处理 UMI episode，因为每个 UMI episode 可能包含较重的视频和轨迹处理。默认启用 resource guard：它会把 worker 数限制在安全范围内，并在服务器负载或可用内存过低时暂停，并一直等到服务器恢复。只有在明确希望超时停止时，才把 `--resource-max-wait-seconds` 设置为正数；长时间服务器运行应保持默认值 `0`。

如果 resource guard 在某个阶段内部停止运行，runner 默认会重试该阶段。每个 episode 完成后都会立即把状态保存到 SQLite，因此重试该阶段时会从该阶段未完成的 episode 继续。

```text
--resource-error-retries          默认 3
--resource-retry-delay-seconds    默认 30
```

`--batch-size` 会限制每次加载进内存的 episode 数量。比如 10000 个 episode 且 `--batch-size 1000` 时，会分 10 批处理，避免一次性把全部 state/metadata 放进服务器内存。每批结束后，流水线会释放该批的 state 列表并执行 Python 垃圾回收。batch 模式不会删除最终报告、SQLite 记录、dashboard 或第 6 阶段的 UMI 处理结果。内存紧张的服务器建议从 `--batch-size 500` 或 `1000` 开始，确认内存稳定后再增大。

`--batch-mode` 控制如何组成 batch：

```text
auto         默认值；选择第 2 或第 3 阶段时自动使用 group-aware batch
fixed        简单固定大小 batch
group-aware  保证第 2/3 阶段的离群统计分组不会被切开
```

Group-aware batching 会避免第 2/3 阶段在不完整分组上计算离群统计。第 2 阶段按 task 分组，因此会把完整 task 放在同一个 batch。第 3 阶段按 task+robot 分组，因此在未选择第 2 阶段时会把完整 task+robot 放在同一个 batch。如果某个完整分组本身大于 `--batch-size`，它会作为一个超出 batch size 的完整 batch 运行，并打印 warning。这是有意行为：优先保证分组统计正确，而不是强行切开分组。

进入详细阶段检查前，runner 会先应用默认质量标签过滤。只有 `metadata.json`
中 `quality.labels` 包含 `完全正常` 的 episode 会被处理。采集员标为其他质量
标签的 episode 会被跳过，并在终端汇总跳过原因。该过滤在状态加载和阶段派发
前执行，因此适用于所有阶段。可用以下参数调整：

```text
--quality-label <label>              处理指定质量标签
--disable-quality-label-filter       完整审计时处理所有质量标签
```

使用 `--force-rerun` 时，会重新计算当前筛选到的 episode 的所选阶段。已有 episode 行会被更新，同一个 `episode_path + phase` 的旧 findings 会被删除并写入新 findings。每次运行开始时，数据库还会按当前 `--roots`、`--date`、`--task`、质量标签过滤和 `--max-episodes` 得到的 episode 集合进行清理，删除当前集合之外的旧 episode 记录，避免旧筛选条件的结果污染当前 dashboard。

如果要继续一个中断的运行，复用相同的 `--db-path` 和 `--output-dir`，并且不要传 `--force-rerun`。当前 resume 路径仍会先扫描输入 root，并加载匹配 episode 的状态，然后才能跳过已完成工作。因此在大型 NAS root 上，即使之前的阶段结果已经在 SQLite 中，重新发现 episode 仍可能需要等待。后续应增加真正的 DB-resume 模式来避免全量发现步骤。

注意：第 2、3 阶段包含组级离群统计；batch 模式下这些组级统计会按批次计算。使用默认的 `--batch-mode auto` 或显式使用 `--batch-mode group-aware`，可以避免这些阶段的离群统计分组被切到不同 batch。第 1 阶段文件完整性检查不受这个影响。

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

发现阶段会跳过隐藏目录，例如 NAS 或同步工具留下的 `.fr-*` 临时目录。这些目
录不会被当作 episode 内容处理，但会作为 `hidden_directory_skipped` finding
写入实时和最终报告。

## 输出

常规输出目录包含：

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
dashboard_data.json
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
    dashboard_data.json
```

实时 dashboard 现在作为独立进程运行。流水线运行时，在另一个 terminal 或
tmux pane 中使用同一个 DB 和输出目录启动：

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/<run>/qa_pipeline.db \
  --output-dir outputs/<run> \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

该进程会写入并刷新：

```text
outputs/<run>/dashboard.html
outputs/<run>/dashboard_data.json
```

`dashboard.html` 是稳定页面 shell，实时数据写在同目录的
`dashboard_data.json` 中。通过 HTTP 访问时，页面会轮询 JSON 并在原页面内
更新，因此自动刷新时不应出现整页空白。`live_dashboard.py --interval` 控制
独立 dashboard 进程的生成间隔；主流水线的 `--live-dashboard-interval` 只控
制运行开始时打印的建议命令。`--port 0` 只更新文件，不启动 HTTP 服务；
`--once` 只生成一次并退出。直接用 `file://` 打开时，浏览器安全限制会阻止
读取 JSON；请使用 `live_dashboard.py --port <port>` 或其他 HTTP 服务访问
输出目录。

`quality_report.csv` 每个 episode 一行。`quality_findings.jsonl` 和 `episode_issues.csv` 每个具体问题一行。正常流水线不生成 Excel；只有需要人工分享时再单独手动生成。

## Dashboard

运行时或运行结束后，打开：

```text
<output-dir>/dashboard.html
```

运行结束后，最终副本还会写入 `<output-dir>/runs/<run-id>/final/dashboard.html`。

如需从服务器更新并访问，请在单独 terminal 或 tmux pane 中运行：

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/qa_20260611/qa_pipeline.db \
  --output-dir outputs/qa_20260611 \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

然后打开：

```text
http://<server-ip>:1234/dashboard.html
```

端口可以换成任意空闲端口。例如 8080 被占用时可使用 1234。如果本机无法直接访问该端口，可以在本机使用 SSH 端口转发：

```bash
ssh -L 1234:localhost:1234 xinzhi@192.168.50.209
```

然后打开 `http://localhost:1234/dashboard.html`。

Dashboard 展示：

- episode 总数；
- `fail`、`needs_review`、`warning` 和 `pass` 数量；
- 主要问题类型；
- 按阶段统计的问题数量；
- 可筛选的 episode 表；
- 可筛选的具体问题表。

## 服务器控制 Dashboard

服务器上推荐长期运行中心控制台：

```bash
python3 QA_Pipeline/scripts/qa_control_dashboard.py \
  --host 0.0.0.0 \
  --port 4131
```

打开：

```text
http://<server-ip>:4131
```

它和上面的 `live_dashboard.py` 不同。`live_dashboard.py` 只展示某个输出目录；
`qa_control_dashboard.py` 是服务器控制台，支持：

- 在页面上启动 date-range 或 task-folder run；
- 设置 `date_from`、`date_to`、`phases`、`workers`、`batch_size`、质量标签过滤；
- 查看 run 实时状态、阶段进度、日志和问题 episode；
- 管理 event listener 的启动、停止、重启和日期过滤；
- 生成/刷新中文工作时段报告；
- 查看设备故障统计和连续失败告警。

Run Detail 里的 `中文质检报告` 可以生成：

- `当前运行累计报告`：按当前 run 的 DB 累计结果生成，适合多日 run 边跑边看；
- 半日报告：按工作时段窗口统计最近或当前半日结果。

报告附件包含：

```text
半日质检报告.md
report.json
核心问题汇总.csv
问题episode清单.csv
采集人员问题占比.csv
采集人员问题episode索引.csv
检测规则说明.csv
处理建议.csv
```

报告正文中类似 `umi(88)`、`pengshasha(21)` 的括号数字表示 finding 条数，不是
去重 episode 数。去重统计看“影响 episode 数”或采集人员部分的“问题 episode”。

Dashboard 中的 `查看检测规则说明` 会打开浮层，显示命中规则的中文说明、判定
标准、阈值和证据字段。规则说明配置在：

```text
QA_Pipeline/configs/report_rule_explanations_zh.json
```

Issues 面板会检测连续失败组合。组合身份为：

```text
task + robot + operator + date
```

date 优先来自 QA DB，缺失时从 episode 路径中的日期目录推断。这样同一
operator 在不同日期用同一机器做同一任务，即使 episode 编号范围相同，也会
作为新的独立问题重新检测。

Reviewer 点击 `标记已解决` 后，按钮会变成 `确认解决？`。第二次点击会立即在
前端隐藏该组合，并在后端把同一 `task + robot + operator + date` 下的当前
unresolved 连续失败段标记为 resolved。如果后续真的再次出现新的连续失败段，
达到阈值后会重新出现在 Issues 列表。

设备故障统计只统计 `fail` 和 `needs_review` finding，并按 collector/device
汇总。若某个设备的问题高度集中在同一 check name，会标记为重点设备风险，便
于优先排查硬件、相机、采集端负载或连接问题。

## 运行中的实时状态

查看最新一次运行的实时摘要，不需要手动输入 run-id：

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611
```

持续刷新显示：

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611 --watch
```

如果要查看某个指定 run：

```bash
python3 QA_Pipeline/scripts/qa_status.py --output-dir outputs/qa_20260611 --run-id server-test-001
```

主流水线会在输出目录写入 `latest_run.txt`，该脚本会自动读取它并找到最新运行目录。需要实时查看详细 issue 时，可先用 `cat outputs/qa_20260611/latest_run.txt` 找到目录，再查看其中的 `issue_events.jsonl`。

HTML dashboard 会在独立 dashboard 进程启动后存在，并随该进程刷新：

```bash
ls outputs/qa_20260611/dashboard.html
```

流水线会在质量过滤、状态加载、batch 规划和阶段执行时打印进度。阶段进度会包
含已耗时、粗略 ETA 和处理速率，避免大型 NAS 运行在发现 episode 后看起来像
没有反馈。

## Excel 报告

Excel 是可选的手动导出，不属于正常流水线输出。这样大规模运行不需要为可能用不到的 workbook 消耗内存和 CPU。手动生成的工作簿比 CSV/JSONL 更适合给非技术人员查看，包含：

- `Summary`：episode 总数、状态统计、finding 严重程度统计；
- `Episodes`：每个 episode 一行；
- `Findings`：每个非 pass finding 一行，包含 details；
- `Issue Counts`：按 check name 统计；
- `Task Status`：按 task 统计状态数量。

如果已有 SQLite 结果数据库，不需要重新运行 QA，也可以手动生成 Excel：

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output outputs/qa_20260612_phase1_5/quality_report.xlsx
```

该功能需要 `datapipeline-env` 中安装 `openpyxl`。手动导出默认有大数据安全限制：超过 100,000 个 episode 的数据库会跳过 Excel，除非明确设置 `QA_EXCEL_MAX_EPISODES=0` 强制导出。

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

重要：如果某个 episode 在较早的已完成阶段失败，后续阶段会跳过该 episode。这样可以避免在已经明显不可用的 episode 上浪费计算资源。只有在明确希望失败后仍继续运行后续阶段时，才使用 `--continue-after-fail`。

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
| `action_modality_singular_name` | 模态/文件夹使用 `action.*` 而不是 `actions.*`，会记录待修复项 | warning |
| `unknown_modality_detected` | 检测到未知模态名称，会记录供复核 | 默认 pass/info |
| `task_robot_mismatch` | metadata/name/path 中的机器人来源与 task 文件夹机器人 token 冲突 | 当前第 1 阶段为 fail |

`observation.image.flow_*` 模态在第 1 阶段文件完整性检查中会被主动忽略。它
是否存在不影响 pass/fail 状态。

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
| `state_csv_row_count_mismatch` | 非触觉状态 CSV 行数与预期行数的差异超过 15% | warning |
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
| `consecutive_drops_outlier` | 最大连续丢帧是 IQR 离群值，或在小样本组中超过兜底 warning 阈值 | needs_review / warning |

配置阈值：

```json
phase3_timestamp.abnormal_fps.loss_fail_ratio = 0.10
phase3_timestamp.abnormal_fps.gain_warning_ratio = 0.10
phase3_timestamp.frame_drops.normal_video_drop_ratio_fail = 0.10
phase3_timestamp.frame_drops.tactile_video_drop_ratio_fail = 0.15
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

性能说明：

第 4 阶段通常是 NAS 上最慢的阶段。它会对每个图像 `video.mp4` 使用 OpenCV 打开 MP4，读取视频属性，然后随机 seek 并解码最多 8 个采样帧。在 NAS 上对压缩 MP4 做随机 seek 代价很高，并且会因为 I/O wait 抬高 Linux load average。如果第 4 阶段很慢或触发 resource guard，建议单独运行第 4/5 阶段，降低 workers，并放宽 load 阈值：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260612 \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --phases 4,5 \
  --workers 2 \
  --batch-size 500 \
  --batch-mode fixed \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 1.20 \
  --resource-check-interval 60 \
  --resource-max-wait-seconds 0 \
  --resource-error-retries 5 \
  --resource-retry-delay-seconds 20
```

继续中断运行时不要加 `--force-rerun`。

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
  --workers 3 \
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
2. 长时间任务建议先在服务器上开启持久终端会话：

```bash
tmux new -s qa_verified
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

这样即使 VS Code SSH 断开或本地电脑冻结，流水线仍会继续运行。使用
`Ctrl-b d` 退出会话，之后用 `tmux attach -t qa_verified` 恢复查看。

3. 从本地仓库根目录将当前仓库部署到服务器：

```bash
rsync -av \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

4. 执行一次 dry-run 发现：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas_homes/xinzhi/Test_Folder_For_DataPipeline \
  --dry-run
```

5. 运行小规模 smoke test：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --phases 1 \
  --max-episodes 1000 \
  --workers 3 \
  --batch-size 500 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --force-rerun \
  --run-id server-smoke-001
```

6. 在单独 terminal 或 tmux pane 中打开 dashboard：

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/server_smoke/qa.db \
  --output-dir outputs/server_smoke \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

然后浏览：

```text
http://<server-ip>:1234/dashboard.html
```

7. 在运行更大规模测试前，先复核 `fail` 和 `needs_review` episode。
8. 只有在理解 smoke test 结果后，再运行更大批次。

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

在服务器上使用保守并发运行当前所有阶段：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260611 \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --phases 1,2,3,4,5,6 \
  --workers 3 \
  --batch-size 500 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 0.65 \
  --force-rerun \
  --run-id full-001
```

从已有数据库生成一次 dashboard 快照：

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/full/qa.db \
  --output-dir outputs/full \
  --once \
  --port 0
```

从已有数据库手动生成 Excel：

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/full/qa.db \
  --output outputs/full/quality_report.xlsx
```

## 安全规则

- 先在复制出的样本或只读 NAS 挂载上运行。
- smoke test 使用 `--max-episodes`。
- 在任何清理或隔离步骤前，先复核 `dashboard.html`、`quality_report.csv` 和 `quality_findings.jsonl`。只有需要时再单独导出 Excel。
- 在本地验证并经过复核前，不要对 NAS 源数据执行裁剪或隔离操作。
- 输出目录应与源 episode 文件夹分开。
- 仅在明确想重新计算所选阶段时使用 `--force-rerun`。它会替换当前筛选到的 episode 和阶段的 findings；运行开始时也会清理当前 episode 集合之外的旧 episode 记录，避免旧筛选条件污染当前 dashboard。
- 继续中断运行时，复用相同数据库和输出目录，并省略 `--force-rerun`。
- 8 核 16GB 服务器不要使用全部核心；先使用 `--workers 2`，确认负载稳定后再谨慎提高。
- NAS 大日期运行使用 `--batch-size 500` 或 `--batch-size 1000`，避免一次加载全部 episode state。
- 第 2/3 阶段运行时保持默认 `--batch-mode auto`。它可以避免 task 或 task+robot 离群统计分组被切到不同 batch。
- 默认 resource guard 会在负载或内存风险过高时暂停。`--resource-max-wait-seconds 0` 表示一直等待到服务器恢复；只有 fail-fast 测试运行才建议设置正数超时。

## 当前限制

- 主 QA Pipeline 只报告分类，不移动或删除数据。
- 静止裁剪规划器只报告裁剪候选，目前还不会裁剪视频或 CSV。
- 第 4 阶段需要 OpenCV；没有 OpenCV 时，流水线会在写入 episode QA 结果前退出。
- 当前 resume 仍会先从输入 root 发现 episode，然后才根据 SQLite 跳过已完成工作。在大型 NAS root 上这一步可能较慢。
- `--workers` 会传给所有已注册阶段。第 1 到第 5 阶段可使用 multiprocessing；第 6 阶段目前会顺序处理 UMI episode。实际 worker 数可能被 resource guard 降低，以保护服务器。
- 部分阈值基于当前样本数据校准，在 NAS 全量规模强制执行前应再次复核。
