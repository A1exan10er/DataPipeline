# Wrist-view static check

`check_static.py` 判断 `left_wrist_view` / `right_wrist_view` 视频中相机/本体是否保持静止。

它不会直接用帧差，因为桌面物体移动也会产生帧差。脚本会：

1. 从视频中均匀抽样起始帧，默认比较每个起始帧和 10 帧后的图像，避免全量解码。
2. 对抽样帧对提取角点、用 LK 光流跟踪，并用 RANSAC 拟合主导 affine 运动。
3. 只有当运动模型有足够跨图像区域的空间覆盖时，才把它当作自身运动。
4. 局部且空间覆盖不足的运动会记为 `local_motion_ignored`，用于忽略场景中被操作的物体。

## Usage

```bash
python3 tools/data/static_check/check_static.py /path/to/episode_0001
python3 tools/data/static_check/check_static.py /path/to/class_or_dataset -o static_report
python3 tools/data/static_check/check_static.py /path/to/video.mp4 --dump-pairs
```

如果要比较更长时间跨度，可以调大 `--pair-gap`；如果想回到跨抽样帧的长距离特征匹配模式，可以设 `--pair-gap 0`。

## Apply static-side filtering

`apply_static_filter.py` 读取 `view_static_summary.csv`，按 `left_wrist_view` / `right_wrist_view`
独立结果裁掉静止侧：

- 一侧 `static`、另一侧 `non-static`：删除静止侧 wrist/tactile 数据，CSV 改成 single-arm 列；
  如果保留的是 right，则将 right wrist/tactile 目录统一重命名为 left。
- 已有视角全是 `static`：删除整个 episode。
- 两侧都是 `non-static`：不处理。

默认是 dry-run，只写计划：

```bash
python3 tools/data/static_check/apply_static_filter.py \
  --report tools/data/static_check/reports/curated_samples_success/view_static_summary.csv \
  --out tools/data/static_check/reports/curated_samples_success/static_filter_dry_run
```

确认计划后再实际修改：

```bash
python3 tools/data/static_check/apply_static_filter.py \
  --report tools/data/static_check/reports/curated_samples_success/view_static_summary.csv \
  --out tools/data/static_check/reports/curated_samples_success/static_filter_apply \
  --apply
```

执行时会生成 `.single_arm.json`，更新 `metadata.json` / `meta/episode.json` 中的 single-arm 信息，
重写 `.checksum_manifest` 和 `checksums.sha256`，并输出 apply summary。

输出：

- `static_check_results.csv`：每个 wrist-view 视频一行。
- `static_check_results.json`：包含 episode 级和视频级结果；加 `--dump-pairs` 会保存每个抽样帧对的诊断。

视频级 `verdict`：

- `static`：未检测到自身位移。
- `self_moved`：检测到全局自身位移，包括平移、旋转、zoom/scale。
- `inconclusive`：可用特征或全局支持不足，无法可靠判断。
- `error`：视频缺失或无法读取。

episode 级结果要求左右两个 wrist view 都是 `static` 才算 `static`；任一侧 `self_moved` 则 episode 为 `self_moved`。

## Calibration

默认阈值按 `--max-side 640` 的缩放后帧设置：

```bash
--max-corner-px 4.0
--max-translation-px 3.0
--max-rotation-deg 1.0
--max-scale-change 0.015
```

如果数据集中的静止视频有更强编码噪声或 rolling shutter，可以适当放宽这些阈值；如果要更严格地捕捉轻微镜头晃动，则降低这些阈值。
