# QA Pipeline 技术摘要

## 概览

QA Pipeline 的目的，是在机器人记录片段进入训练集之前完成质量检查。它会检测 `episode_*` 文件夹，在 `outputs/qa_pipeline.db` 中为每个记录片段创建或加载一条 SQLite 状态记录，然后按线性顺序执行第 1 阶段到第 5 阶段。每个阶段开始前，已经在前序阶段得到 `fail` 的记录片段会被跳过；这样结构明显损坏、时间明显不合理的数据，不会继续消耗后续视频和机器人状态检查的时间。

每个阶段都会把检查结果写回 SQLite。全部阶段结束后，运行器会为每个记录片段计算最终状态，并导出 `outputs/quality_report.csv`、`outputs/quality_findings.jsonl` 和 `outputs/quality_summary.md`。`quality_report.csv` 是按记录片段汇总的紧凑视图，`quality_findings.jsonl` 保存详细检查日志，`quality_summary.md` 会按状态、任务、操作员、机器人和问题类型对结果分组。

## 状态和严重程度系统

流水线使用四种状态。`pass` 表示没有发现有意义的问题。`warning` 表示记录片段大概率可用，但存在质量风险。`needs_review` 表示数据可能有效，但训练前应由人工检查。`fail` 表示记录片段被认为不安全，或不适合用于训练。

严重程度描述问题影响范围。`critical` 总是会导致记录片段变成 `fail`，通常意味着必需文件无法读取，或数值不可能成立。`major` 在检查本身失败时可能导致 `fail`，也可能用于严重但不一定致命的 `warning` 或 `needs_review` 项。`minor` 通常会变成 `warning` 或 `needs_review`。`info` 用于记录不阻塞流程的观察结果。

## 第 1 阶段 — 结构和元数据检查

目的：验证一个记录片段是否具备预期的文件夹结构、元数据和必需模态文件。

输入：记录片段目录、`metadata.json`、`.checksum_manifest`、图像模态文件夹、`video.mp4`、`timestamps.csv`，以及状态和动作模态中的 `data.csv`。

方法：第 1 阶段检查文件夹名是否以 `episode_` 开头，`metadata.json` 是否存在且是合法 JSON，以及 `task_key`、`episode_index`、`duration_seconds`、`total_frames`、`modalities`、FPS 和 `quality` 等必需元数据字段是否存在。它会比较元数据里的模态名称和实际文件夹；对于特殊的 `actions` 元数据键，会接受 `action.*` 和 `actions.*` 文件夹。之后，它会确认必需文件存在且非空。图像流需要 `video.mp4` 和 `timestamps.csv`；光流图像流只需要 `video.mp4`；状态和动作流需要 `data.csv`。

分类：元数据缺失或无效、模态文件夹缺失、必需文件缺失或为空，都会得到 `fail`。`.checksum_manifest` 缺失、父目录结构不标准、质量标签缺失，会产生 `warning`。这个阶段很快，因为它主要读取元数据和文件系统属性。

## 第 2 阶段 — 时长和数量一致性

目的：发现记录时长或行数、帧数与元数据不一致的情况。

输入：`metadata.json`、图像 `timestamps.csv`、状态 `data.csv`，以及元数据中各模态的数量字段。

方法：第 2 阶段检查 `duration_seconds` 和总帧数是否为正数；时长低于 5 秒会得到 `fail`。它还会检查 `duration_seconds * fps` 是否大致匹配 `total_frames`。如果误差超过 10%，说明元数据和实际记录不一致。它会用 10% 容差比较图像时间戳行数和 `total_frames`，并用 15% 容差比较状态 CSV 行数和时长乘以 FPS 后的预期值。它还会检查元数据中各模态的数量差距：3 帧以内可以接受，4 到 10 帧产生 `warning`，更大的差距需要 `needs_review`。分组检查会在同一任务内比较时长：超过 3 IQR 的极端任务离群值需要 `needs_review`，低于任务中位数 20% 会得到 `fail`，低于 40% 需要 `needs_review`，高于 250% 也需要 `needs_review`。

分类：不可能成立的时长或帧数元数据会得到 `fail`。较大的时间戳数量不匹配会得到 `fail`。状态行数不匹配会产生 `warning`。这个阶段速度为快到中等：它读取 CSV 行数，但不解码视频。

## 第 3 阶段 — 时间戳同步

目的：验证图像时间戳流是否有序、规律，并且彼此同步。

输入：图像 `timestamps.csv`、元数据中的 `frame_integrity`，以及用于一致性检查的可选 `timestamps_raw.csv`。

方法：对除光流以外的图像模态，第 3 阶段读取 `is_new == 1` 的 `timestamp_ms` 行。它检查时间戳是否严格递增、是否重复、实际频率是否偏离预期 FPS，以及各模态的起止时间是否对齐。起点或终点差距超过 500 ms，表示不同相机没有覆盖同一段时间。它使用 `frame_integrity` 判断 `frame_drop_ratio` 是否至少为 10%；从物理意义上说，这表示每 10 个预期帧中，少于 9 个包含新的视觉信息。当 `timestamps_raw.csv` 和处理后的 `timestamps.csv` 相差超过 2 行时，会产生 `warning`。分组检查会在任务加机器人分组内标记异常 FPS 和异常长的连续丢帧段。

分类：频繁的非单调时间戳或 `duplicate_timestamps` 可能导致 `fail`；较小比例会产生 `warning` 或 `needs_review`。大间隔超过中位间隔的 5 倍会产生 `warning`，超过 20 倍会得到 `fail`。这个阶段成本为中等，因为它读取时间戳行并执行分组统计。

## 第 4 阶段 — 视频健康检查

目的：确保每个相机视频可以解码，并包含合理的视觉数据。

输入：每个非光流的 `observation.image.*/video.mp4`、元数据中的 `total_frames`、`duration_seconds`、相机元数据，以及可选的 `config.csv`。

方法：第 4 阶段使用 OpenCV 打开每个视频，读取 `frame_count`、FPS、宽度和高度，并用 10% 容差将帧数和时长与元数据比较。它会根据元数据或相机配置检查分辨率；如果实际存储帧更高且看起来是填充，则允许这种情况。它在整个视频中最多采样 8 帧，测量亮度，并比较相邻采样灰度帧。非常暗或非常亮的采样帧表示黑屏或白屏视频。如果所有采样帧差异都低于 1.0，则触发 `video_frozen`，说明视频看起来被冻结。仅对 `arx5`，它会比较采样的 `observation.image.left_wrist_view` 和 `observation.image.right_wrist_view`；如果两个腕部视角在超过 80% 的采样对中都保持静止，则该记录片段会作为可能的操作员空闲得到 `fail`。

分类：视频无法打开、帧数无法读取、帧数不匹配、`video_frozen`，或两个 `arx5` 腕部视角同时静止，都会得到 `fail`。分辨率或时长不匹配会产生 `warning`。异常采样帧需要 `needs_review`；如果超过一半采样帧异常，则会得到 `fail`。这个阶段较慢，因为需要定位并解码视频帧，但支持多进程。

## 第 5 阶段 — 机器人状态合理性

目的：检测物理上不合理的机器人运动，以及较长的操作员空闲时间。

输入：`actions.joint_position/data.csv`、`observation.state.joint_position/data.csv`、可选的 `observation.state.joint_velocity/data.csv`，以及末端执行器位姿 CSV。

方法：第 5 阶段解析数值列，并识别 `arx5` 双臂列（`left_j*`、`right_j*`）或 `flexiv` 列（`j1`、`joint_*.pos`）。它检查 NaN/Inf、时间戳单调性、关节和夹爪限位、逐帧关节和夹爪步长、实测或估计速度、加速度、抖动，以及末端执行器位置跳变。`arx5`、`flexiv` 和 `ur` 使用不同限制；未知机器人会回退到 `arx5` 默认值。静止检测使用关节位置行：如果所有非夹爪关节在超过 5 秒缓冲时间里都小于一个很小的运动阈值，则记录额外空闲时间；如果额外空闲超过记录片段时长的 20%，则需要 `needs_review`。相关配置来自 `ROBOT_CONFIGS` 和 `STANDSTILL_BUFFER_MS`。

分类：解析失败、NaN/Inf、频繁时间戳违规或高抖动，可能导致 `fail`。关节限位、过大步长、高速度和末端执行器跳变通常需要 `needs_review`；高加速度会产生 `warning`。这个阶段成本为中等到较慢，因为它会加载数值 CSV，并且可以并行运行。

## 补充工具：帧对齐（align_frames.py）

这个独立脚本会从实际文件重新检查帧数和行数对齐，而不是只依赖元数据。它读取图像 `timestamps_raw.csv` 或 `timestamps.csv`，以及状态和动作模态中的 `data_raw.csv` 或 `data.csv`。它为每个记录片段选择一种文件策略：只有当所有被检查的模态都有原始文件时才使用原始文件，否则使用处理后文件，避免混用原始和处理后的数量。它排除光流流和触觉状态流，因为这些流行为不同，并且经常多出一行。

如果所有数量匹配，状态是 `pass`。如果差距是 1 到 3，状态是 `needs_trim`；使用 `--trim` 时，它会在原文件旁边写入 `timestamps_trimmed.csv` 或 `data_trimmed.csv`，不会覆盖原文件。差距超过 3 是 `fail`。该工具还会从两个腕部视角视频中建议头部和尾部裁剪点。它每 30 帧采样一次，从最多 10 个全视频样本计算自适应运动阈值，然后用五个样本的滑动窗口扫描开头和结尾区域；窗口内至少 80% 必须静止，并且持续至少 5 秒。因为需要解码腕部视频，它比单纯数量检查更慢，但支持 `--workers`。

## 补充工具：相机清晰度检查（check_tactile_focus.py）

这个独立脚本会检查每个非光流图像相机的清晰度，包括 RGB 和触觉相机。对每个视频，它会在大约 40%、50%、60% 位置采样三帧中段帧，并在结尾采样三帧尾部帧。它计算 Laplacian variance，用来衡量图像边缘的清晰程度：模糊帧的边缘较软，分数较低；清晰帧的边缘更锐利，分数更高。当前阈值是 50.0。

CSV 会记录 `camera_type`、帧位置、分数、模糊标记、宽度和高度。摘要会分开统计 RGB 和触觉相机，因为触觉图像的低分可能表示没有接触，而不一定是光学模糊。模糊记录片段数量只基于 RGB。这个工具成本为中等到较慢，因为每个相机要解码 6 帧，并且支持多进程。
