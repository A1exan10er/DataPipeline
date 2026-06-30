# DataPipeline

<p align="right">
  <a href="README.md"><kbd>中文</kbd></a>
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

DataPipeline 包含用于验证机器人和 UMI episode 数据集的 QA Pipeline 以及配套工具。当前主流程是“先报告、后处理”：流水线会对 episode 分类并生成结构化报告，但主 QA 运行过程中不会删除源数据、移动 episode，也不会裁剪视频。

更详细的阶段规则和命令示例见：

- `QA_PIPELINE_USER_GUIDE_ZH.md`
- `QA_PIPELINE_USER_GUIDE.md`

## 仓库结构

```text
DataPipeline/
  QA_Pipeline/            主多阶段 QA Pipeline
  DataProcessUMI/         UMI 验证、预处理和 world-frame 导出
  UMI_Data_Validation/    额外的 UMI 验证/原型代码
  Documents/              参考文档和 PDF
  Werkzeuge/              额外分析工具和文档
  Test_Folder_For_DataPipeline/
                          本地测试样本；部署到服务器时应排除
  datapipeline-env/       本地/服务器 Python 虚拟环境
```

运行生成的结果通常写入 `outputs/`，不应提交到 Git。

## Episode 目录结构

扫描器会查找名称以 `episode_` 开头的文件夹。

旧版路径：

```text
<root>/<task>/<date>/<operator>/episode_...
```

新版 robot/collector 路径：

```text
<root>/<task>/<robot_type>/<collector_id>/<date>/<operator>/episode_...
```

典型 episode 内容：

```text
episode_0001/
  metadata.json
  observation.state.joint_position/data.csv
  actions.joint_position/data.csv
  observation.image.<camera>/timestamps.csv
  observation.image.<camera>/video.mp4
```

机器人类型优先从 `metadata.json` 推断，其次从 episode 名称推断，必要时再从路径结构推断。UMI episode 可能只有类似 `episode_0094` 的简单名称，因此 metadata 和路径上下文都很重要。

## 环境设置

从仓库根目录激活虚拟环境：

```bash
source datapipeline-env/bin/activate
```

主要 Python 依赖列在：

```text
QA_Pipeline/requirements.txt
```

当前重要依赖：

- `opencv-python-headless`：第 4 阶段视频检查；
- `scipy`：UMI 处理；
- `openpyxl`：仅手动 Excel 导出需要，正常 QA 运行不需要；
- `pytest`：仅开发/回归测试需要，正常 QA 运行和 dashboard 不需要；
- 主机上的 `ffmpeg` 和 `ffprobe`：UMI 视频处理。

将 Python 依赖安装到仓库虚拟环境：

```bash
python3 -m pip install -r QA_Pipeline/requirements.txt
```

如果需要运行第 6 阶段 UMI 处理，Ubuntu 服务器还需要安装 FFmpeg：

```bash
sudo apt update
sudo apt install -y ffmpeg
```

## QA Pipeline

入口脚本：

```text
QA_Pipeline/scripts/run_pipeline.py
```

阶段：

```text
1  结构、metadata、必需文件、标签、机器人/task 不匹配检查
2  时长、帧数、行数、task 级时长离群检查
3  时间戳、FPS、丢帧和多图像模态起止同步检查
4  视频健康：可打开性、视频属性、黑/白/冻结采样帧
5  机器人状态/action 合理性和静止检查
6  UMI 专用验证、预处理和 world-frame 导出
```

所有阶段都可以通过 `--phases` 选择性运行。

每个阶段的详细判定规则见 `QA_PHASE_DECISION_RULES.md`。

默认情况下，流水线会在进入各阶段前读取 `metadata.json`，只处理
`quality.labels` 中包含 `完全正常` 的 episode。采集员已经标为其他质量标签
的 episode 会被跳过，以减少无效检查和服务器负载。需要完整审计时可使用
`--disable-quality-label-filter`，或用 `--quality-label` 指定其他标签。

扫描阶段会跳过隐藏目录，例如 `.fr-*`。这些目录不会作为 episode 处理，但会
作为 `hidden_directory_skipped` 写入实时和最终报告，便于排查 NAS 或同步工具
留下的临时目录。

运行一个小规模本地测试：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots Test_Folder_For_DataPipeline \
  --db-path outputs/test_run/qa_pipeline.db \
  --output-dir outputs/test_run \
  --phases 1,2,3 \
  --max-episodes 10 \
  --workers 2 \
  --force-rerun
```

运行保守的服务器日期扫描：

```bash
python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260612 \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --phases 1,2,3,4,5 \
  --workers 3 \
  --batch-size 5000 \
  --batch-mode auto \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 1.20 \
  --resource-check-interval 60 \
  --resource-max-wait-seconds 0 \
  --resource-error-retries 5 \
  --resource-retry-delay-seconds 20 \
  --force-rerun
```

如果要继续一个中断的运行，复用相同的 DB/output 路径，并省略 `--force-rerun`。Episode 状态会增量保存，因此加载到已有状态后，已完成阶段会被跳过。

## Batch 和 Resume

`--batch-size` 限制每次加载到内存中的 episode state 数量。每个 batch 结束后，流水线会释放该 batch 的 state 列表并执行 Python 垃圾回收。

推荐使用 `--batch-mode auto`。当选择第 2 或第 3 阶段时，它会使用 group-aware batching，避免在不完整 task 或 task+robot 分组上计算离群统计。如果某个完整分组本身大于 `--batch-size`，该分组会作为一个超出 batch size 的完整 batch 运行，并打印 warning。

每次运行会先根据当前 `--roots`、`--date`、`--task`、质量标签和
`--max-episodes` 选出本次 episode 集合，然后把 SQLite 中不属于本次集合的
旧 episode 记录清理掉，避免旧结果污染当前 dashboard。Resume 仍会先扫描输
入 root，然后再从 SQLite 加载已保存状态；在很大的 NAS root 上，即使之前已
有记录，重新发现 episode 仍可能需要时间。

## Resource Guard

Resource guard 默认启用。它可以：

- 将请求的 worker 数降低到安全值；
- 在负载或内存不安全时暂停，默认一直等待到恢复；
- resource-guard stop 后重试当前阶段。

常用选项：

```text
--min-free-mem-gb
--max-load-ratio
--resource-check-interval
--resource-max-wait-seconds
--resource-error-retries
--resource-retry-delay-seconds
```

第 4 阶段通常是 NAS 上最慢的阶段，因为它会打开很多 MP4 文件并进行随机 seek。如果第 4 阶段导致高负载，可以单独运行并减少 worker：

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

从中断处继续运行时不要添加 `--force-rerun`。

## 报告和 Dashboard

常规输出：

```text
quality_report.csv
quality_findings.jsonl
quality_summary.md
dashboard.html
dashboard_data.json
qa_pipeline.db
```

实时监控还会写入：

```text
<output-dir>/runs/<run-id>/
  run_status.json
  phase_status.jsonl
  issue_events.jsonl
  episode_issues.csv
  live_summary.md
  dashboard.html
  dashboard_data.json
```

查看终端实时状态：

```bash
python3 QA_Pipeline/scripts/qa_status.py \
  --output-dir outputs/qa_20260612_phase1_5 \
  --watch
```

启动独立 dashboard 进程：

```bash
python3 QA_Pipeline/scripts/live_dashboard.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output-dir outputs/qa_20260612_phase1_5 \
  --interval 5 \
  --max-episodes 5000 \
  --max-findings 10000 \
  --port 1234
```

然后打开：

```text
http://<server-ip>:1234/dashboard.html
```

`dashboard.html` 是实时 shell，数据在旁边的 `dashboard_data.json` 中。通过
HTTP 访问时，页面默认每 5 秒读取一次 JSON 并局部更新内容，不再整页刷新，
因此自动刷新时不应出现空白页。`live_dashboard.py --interval` 控制独立进程
的生成间隔；主流水线的 `--live-dashboard-interval` 只控制运行开始时打印的
建议命令。`--port 0` 只写 dashboard 文件，不启动 HTTP 服务；`--once` 只生成
一次并退出。直接用 `file://` 打开时，浏览器安全限制会阻止读取 JSON；请使用
`live_dashboard.py --port <port>` 或其他 HTTP 服务访问输出目录。

### 服务器控制 Dashboard

服务器长期运行推荐使用中心控制台：

```bash
python3 QA_Pipeline/scripts/qa_control_dashboard.py \
  --host 0.0.0.0 \
  --port 4131
```

然后打开：

```text
http://<server-ip>:4131
```

该控制台与 `live_dashboard.py` 不同：`live_dashboard.py` 只展示单个输出目录的
只读 HTML；`qa_control_dashboard.py` 是服务器级控制台，负责：

- 从页面启动 date-range 或 task-folder run；
- 设置 `date_from`、`date_to`、`phases`、`workers`、`batch_size`、质量标签过滤等参数；
- 查看当前 run 的阶段进度、日志、episode/finding 数和中文报告；
- 管理 event listener 的启动、停止、重启和日期过滤；
- 汇总最近 event listener job 的问题、设备故障和采集人员/设备组合；
- 展示“连续失败组合”告警，并允许 reviewer 标记已解决。

控制台启动 run 时，默认仍遵守 `quality.labels` 过滤，只处理 `完全正常` 数据；
需要完整审计时可勾选或传入禁用质量标签过滤。

### 中文工作时段报告

中文报告生成脚本：

```bash
python3 QA_Pipeline/scripts/generate_work_session_report.py \
  --db-path outputs/<run>/qa.db \
  --output-dir outputs/<run>/reports/work_sessions \
  --session current
```

输出目录包含：

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

在服务器控制台的 Run Detail 中，`中文质检报告` 可选择：

- `当前运行累计报告`：覆盖当前 run 已经写入 DB 的全部结果，适合多日 run 边跑边看；
- `最近结束的工作半日`、`当前工作半日`、`今天上午`、`今天下午`：按更新时间窗口生成半日报告。

报告中“主要任务/主要机器人/主要采集人员”后面的括号数字表示该类别下的
finding 条数，不是去重 episode 数；去重 episode 数见“影响 episode 数”或
采集人员部分的“问题 episode”。

报告还会写出中文检测规则说明。Dashboard 中的 `查看检测规则说明` 按钮会弹出
浮层，直接展示命中的问题类型、中文说明、判定标准、阈值和证据字段，reviewer
无需单独打开 CSV。当前规则说明配置位于：

```text
QA_Pipeline/configs/report_rule_explanations_zh.json
```

### Event Listener 与连续失败告警

Event listener 用于持续监听新采集 episode 并提交 QA job。服务器控制台会显示
pending/running/done job 数、最近问题 job、问题类型汇总，以及工作时段中文报告。

控制台左侧 Issues 面板会检测明显连续失败：

- 以 `task + robot + operator + date` 为组合；
- 连续 `fail` episode 达到阈值时出现告警，目前阈值为 5；
- date 来自 QA DB 的 `date` 字段，缺失时从路径
  `<verified>/<task>/<robot>/<collector>/<date>/<operator>/episode_...` 推断；
- 因此同一采集人员、同一任务、同一机器在不同日期出现相同 episode 编号范围，
  会作为新的独立问题重新检测。

Reviewer 点击 `标记已解决` 后按钮会变成 `确认解决？`；第二次点击才真正提交。
提交后前端会立即隐藏该组合，后端把同一 `task + robot + operator + date` 下
当前 unresolved 连续失败段标记为 resolved。后续如果同一天或新日期又产生新的
连续失败段，达到阈值后会重新出现在 Issues 列表。

### 设备故障统计

中文报告和 event listener 报告会生成设备/采集端维度的 failure summary：

- 只统计 `fail` 和 `needs_review` finding；
- 按 collector/device 的问题总数排序；
- 当某个问题类型占该设备问题的大头时，会标记为重点设备风险；
- Dashboard 主页面显示前若干设备摘要，详情页显示完整 breakdown。

Excel 不属于正常流水线输出。如需从已有 DB 生成 Excel，不需要重新运行 QA：

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output outputs/qa_20260612_phase1_5/quality_report.xlsx
```

Excel 工作簿包含 summary、episodes、exact findings、issue counts 和 task status counts 等 sheet。该功能需要虚拟环境中安装 `openpyxl`，并且应作为单独命令按需运行。

## UMI 处理

第 6 阶段将 `DataProcessUMI` 集成进 QA Pipeline。选择方式：

```bash
--phases 6
```

UMI 检测会使用 metadata 中的 robot 值、episode 名称中的 robot token，以及路径上下文。非 UMI episode 会被跳过，并产生 pass/info finding。

第 6 阶段不运行 IK。它执行 UMI raw-data assessment、trajectory preprocessing 和 world-frame export。UMI 处理可能较慢，因为它可能需要打开视频、复制/转换 episode 文件夹，并使用 FFmpeg。

第 6 阶段默认输出目录：

```text
outputs/umi_processed/
```

## 静止裁剪规划器

静止裁剪规划器与主 phase runner 分离。它只生成报告，不裁剪视频，也不重写 CSV。

```bash
python3 QA_Pipeline/scripts/plan_standstill_trim.py \
  --roots Test_Folder_For_DataPipeline \
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

## 服务器部署说明

部署到服务器时，应排除测试样本和生成输出。服务器也需要 `datapipeline-env` 或等价依赖环境。

长时间任务建议在服务器上的 `tmux` 或 `screen` 中运行，避免 VS Code SSH 或本
地电脑断开后中断流水线：

```bash
tmux new -s qa_verified
cd /home/xinzhi/DataPipeline
source datapipeline-env/bin/activate
```

示例：

```bash
rsync -azv \
  --exclude '.git/' \
  --exclude '.vscode/' \
  --exclude 'Test_Data/' \
  --exclude 'NAS_Sample_Data/' \
  --exclude 'Test_Folder_For_DataPipeline/' \
  --exclude 'outputs/' \
  --exclude 'qa_feature_test/' \
  --exclude 'qa_umi_test/' \
  ./ \
  xinzhi@192.168.50.209:~/DataPipeline/
```

## 旧版工具

仓库中仍保留一些独立旧工具：

- `clean_invalid_episodes.py`
- `run_cleanup.sh`
- `annotate_standstill.py`
- `correct_teleop_folders.py`
- `Werkzeuge/` 下的工具

这些工具应视为独立工具。部分工具会原地修改文件或移动文件夹，因此在生产数据上运行前，应先使用 dry-run 或复制数据后验证。

## 安全规则

- QA Pipeline 本身是 report-first，不修改源 episode。
- 仅在明确想重新计算所选阶段时使用 `--force-rerun`。
- 输出目录应与源 episode 文件夹分离。
- 在任何 cleanup、quarantine 或删除步骤前，先复核 `dashboard.html`、`quality_report.csv` 和 `quality_findings.jsonl`。如需 Excel，可用单独导出命令生成。
- 共享服务器上不要占满所有 CPU 核心；先保守设置 worker，再根据负载和内存情况调整。
