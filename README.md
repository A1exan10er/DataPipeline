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
- `openpyxl`：Excel 报告导出；未安装时 QA 仍会完成，但会跳过 `.xlsx`；
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
3  时间戳、FPS、丢帧、task+robot 级时间离群检查
4  视频健康：可打开性、视频属性、黑/白/冻结采样帧
5  机器人状态/action 合理性和静止检查
6  UMI 专用验证、预处理和 world-frame 导出
```

所有阶段都可以通过 `--phases` 选择性运行。

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
  --live-dashboard-interval 5 \
  --min-free-mem-gb 4.0 \
  --max-load-ratio 1.20 \
  --resource-check-interval 60 \
  --resource-max-wait-seconds 15 \
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
- 在负载或内存不安全时暂停；
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
  --resource-max-wait-seconds 15 \
  --resource-error-retries 5 \
  --resource-retry-delay-seconds 20
```

从中断处继续运行时不要添加 `--force-rerun`。

## 报告和 Dashboard

常规输出：

```text
quality_report.csv
quality_report.xlsx
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

启动 dashboard 服务：

```bash
python3 -m http.server 1234 --directory outputs/qa_20260612_phase1_5
```

然后打开：

```text
http://<server-ip>:1234/dashboard.html
```

`dashboard.html` 是实时 shell，数据在旁边的 `dashboard_data.json` 中。通过
HTTP 访问时，页面默认每 5 秒读取一次 JSON 并局部更新内容，不再整页刷新，
因此自动刷新时不应出现空白页。间隔可用 `--live-dashboard-interval` 调整。
直接用 `file://` 打开时，浏览器安全限制会阻止读取 JSON；请使用
`python3 -m http.server` 服务输出目录。

从已有 DB 生成 Excel，不需要重新运行 QA：

```bash
python3 QA_Pipeline/scripts/export_excel_report.py \
  --db-path outputs/qa_20260612_phase1_5/qa_pipeline.db \
  --output outputs/qa_20260612_phase1_5/quality_report.xlsx
```

Excel 工作簿包含 summary、episodes、exact findings、issue counts 和 task status counts 等 sheet。该功能需要虚拟环境中安装 `openpyxl`；如果缺失，主流水线会打印 warning 并继续生成 CSV、JSONL、Markdown 和 dashboard。

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
- 在任何 cleanup、quarantine 或删除步骤前，先复核 `dashboard.html`、`quality_report.xlsx`、`quality_report.csv` 和 `quality_findings.jsonl`。
- 共享服务器上不要占满所有 CPU 核心；先保守设置 worker，再根据负载和内存情况调整。
