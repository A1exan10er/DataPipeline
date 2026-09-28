# 本地人工标注与对照验证

这份流程只产生本机验证证据。候选样本不是准确率证据，脚本耗时不是人工复核耗时；任何结果都不授权服务器部署、操作员通知或机器人动作。

## 打开已运行的预览

- 合成案例：<http://127.0.0.1:1344/pipeline-review>；片段质检：<http://127.0.0.1:1344/>。
- 真实收件 / 历史回放：<http://127.0.0.1:1345/pipeline-review>。
- 本地测试账号：`local-reviewer`，密码：`local-preview-only`。这是隔离预览的账号，不是生产账号。
- 点击顶部监听状态，进入「运行与验收」。真实预览的「准备独立人工标注」提供经过会话认证的 CSV、输入清单和 20 个任务下载；「下载完整证据」提供数据库快照 ZIP。

当前已有 200 个内容指纹固定的真实候选，尚无已确认人工标签。两条历史录制已完成本地处理，但必要证据不足，仍为待检查。历史回放和合成案例不计入 30 条实时收件。

## 独立标注

1. 先保留下载的完整证据 ZIP 和原始 `holdout.json`。在独立查看视频、原始位姿和采集上下文后标注，避免先看新旧检测器预测。负责人确认样本从未用于调参，并在清单的 `holdout_attestation` 填写可核查说明。
2. CSV 保留全部行以及 `episode`、`sha256`。填写 `label`（`normal`、`minor`、`major`）、`review_needed`（`true` 或 `false`）、`reviewer`、`label_status` 和 `notes`。已确定的行用 `confirmed`；争议用 `disputed`，未知用 `unlabeled`，不得删除争议行。
3. 目标至少 200 条确认样本，其中大幅漂移 ≥30、小幅漂移 ≥30、正常 ≥100。现有 200 候选不保证满足分布。若数量不足，应先冻结检测器，再补充未用于调参的真实样本；不把争议强行算作正常。
4. 大幅漂移漏检、证据缺失、输入变化及提示负担分别统计。53 个开发样本仅用于回归，评估按内容哈希排除它们。降低复核提示时仍须保留小幅漂移观察记录。

以下命令在 `/home/tianyu/Projects/DataPipeline` 执行。示例输出不覆盖原始清单。

```bash
datapipeline-env/bin/python scripts/compare_trajectory_versions.py labels-export \
  --manifest /home/tianyu/Projects/QualityCheck/local-data/receipt-trial/benchmark/holdout.json \
  --output /tmp/human-labels.csv

# 人工编辑 /tmp/human-labels.csv，再导入；输入哈希、重复/缺失行和确认人会被验证。
datapipeline-env/bin/python scripts/compare_trajectory_versions.py labels-import \
  --manifest /home/tianyu/Projects/QualityCheck/local-data/receipt-trial/benchmark/holdout.json \
  --labels /tmp/human-labels.csv \
  --output /tmp/holdout-labeled.json

# 由负责人在 /tmp/holdout-labeled.json 填写 holdout_attestation 后运行。
datapipeline-env/bin/python scripts/compare_trajectory_versions.py evaluate \
  --manifest /tmp/holdout-labeled.json \
  --output /tmp/comparison.json
```

这个对照是 **同一检测器、灵敏度 1.0 对 0.5**，不是重建缺失的历史检测器代码。报告固定完整有效配置、检测器源文件 SHA256、输入清单 SHA256、版本和主机条件。相同输入必须在相同硬件条件下比较；输入变化直接报告失败，不静默剔除。若大幅漂移漏检为零，仍须同时报告样本量；有限样本不能证明普遍零漏检。小幅漂移是否可供机器人安全执行仍需另行授权的受控验证。

导入成功后，如需让验收界面显示这次人工结果，先归档旧 `benchmark`，再将确认后的清单和报告分别放入 `benchmark/holdout.json`、`benchmark/comparison.json`，并重新导出 `benchmark/human-labels.csv`。界面会读取实际数量；未确认的内容始终不算通过。

## 人工界面对照

原始 QualityCheck 界面固定为 `07b61383650120cd0c48340dcc3ab55be6bcfe84`。新版本用 `docs/evidence/focused-review/source-provenance.json` 的源文件哈希清单固定，不能只用工作目录的 Git HEAD 代表未提交实现。原版如需启动，也必须使用独立本地目录和数据库，不接生产服务。

下载 `usability-worksheet.json`，按指定 AB / BA 顺序各完成 10 个任务。A 是固定的原版，B 是新界面。记录同一复核员在同一硬件上实际完成每个任务的秒数、决定错误数以及是否找到了支持证据。保存复核员、原版版本和新版本快照。不要填写脚本测量值来代替人工数据。

```bash
datapipeline-env/bin/python scripts/compare_trajectory_versions.py usability \
  --manifest /tmp/usability-worksheet-completed.json \
  --output /tmp/usability-report.json
```

报告要求 20 个完整交叉任务；缺少人工数据会返回 `insufficient`。目标是提示负担降低 ≥30%（检测器比较），以及复核时间降低 ≥20% 且错误和证据可访问性不退步（界面对照）。未达目标应保留实际结果，不改动标签来追求通过。

## 实时验收的独立条件

只读收件账本需要新的记录及持续推进的监听心跳，之后系统才冻结最近活跃的两台 UMI 设备和起始游标。至少 30 条新的范围内收件须逐条对应作业、重试、替代关系和当前结果。重复读取不算新收件，旧作业被替代仍保留记录。

收件到本地发现、排队、处理、可见摘要回执、片段证据显示是不同时间含义。没有测量不能当作零秒；本地显示不是操作员 PC 通知回执。当前上游心跳停在 2026-09-28 13:30:13（中国时间），实时试运行未开始。需要现有上游收件服务恢复持续活动；本次实现不会改动或启动该服务。
