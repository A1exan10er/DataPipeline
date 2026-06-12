# 轨迹打标签与预处理（preprocess / preprocess_trajectory）

`smooth_assessment.py` 只**判定** `actions.eef_pose` 轨迹属于五类标签中的哪一类；
`preprocess_trajectory.py` 在其判定之上**动手处理**，按类别产出“干净”的 episode
（或在数据不可挽救时拒绝输出）。五类评估标签归并为**四种处理方式**：

| 评估标签 | 处理类别 `category` | 动作 |
| --- | --- | --- |
| `smooth`（平滑轨迹） | `passthrough` | **原样输出**：数据逐字节复制，仅写入 metadata 溯源块。 |
| `recoverable`（可恢复轨迹） | `interpolate` | **插值修复**每个可恢复突变段，长度不变。 |
| `middle_smooth` / `middle_recoverable`（中部平滑 / 中部可恢复） | `interpolate_crop` | 先**插值**中部仍保留的可恢复段，再**裁剪**首尾不可恢复段。 |
| `unrecoverable`（不可恢复轨迹） | `reject` | 标注为**不可用**，不输出任何数据。 |

> 输入路径形式、镜像输出布局与 `assessment/validate_raw_data.py` /
> `smooth_assessment.py` **完全一致**（脚本直接复用其 `find_episode_dirs` /
> `episode_output_layout` / IO 辅助，并复用 `smooth_assessment` 的检测与分类逻辑）。

## 四类处理逻辑

### 1. 平滑轨迹 → 原样输出（passthrough）
整段无突变，无需任何修改。脚本复制整个 episode，**轨迹 / 视频 / gripper 等数据保持逐字节不变**，
只在 `metadata.json` 追加 `preprocessing` 溯源块并据此重算校验和。

### 2. 可恢复轨迹 → 插值修复（interpolate）
可恢复突变段是末端追踪器的**短暂瞬跳**（出去又弹回）。因为运动连续，突跳帧不含可用信息，
所以**丢弃**每个可恢复段（逐设备、左右独立）内的位姿采样，**用突变前后的好点重建**它们——
即“排除异常点、用可恢复突变前后的点做插值”。

- **插值算法**：分量独立的 **PCHIP（分段三次 Hermite，保形单调、无过冲）**。把某侧
  可恢复段的所有帧从支撑集中剔除，对其余好帧拟合 PCHIP，再在被剔除帧处求值。
  这样每个缺口都由其**两侧的真实采样**插出，不会在突跳处过冲。
- **修复对象**：被影响那一侧的末端**位置 (x,y,z) 与 6D 旋转 (r1..r6)** 都修复
  （旋转修复后做 Gram–Schmidt 重正交化，保证仍是合法旋转）。gripper / 触觉等**不动**——
  突跳是追踪器伪影，与夹爪无关。
- `actions.eef_pose` 与 `observation.state.eef_pose` 用同一缺口掩码同步修复，保持动作与观测一致。

> 说明：评估器的“recoverable”同时会命中**真实的快速往返运动**（>0.35m/0.5s 且回到容差内）。
> 对这类真实运动，PCHIP 用其本身的真实邻点重建，几乎不改变数据；只有真正的瞬跳才会被抹平。
> 因此本步骤“修复异常、保留真实运动”，但不会把真实的高速运动强行变慢。

### 3. 中部平滑 / 中部可恢复 → 插值 + 裁剪（interpolate_crop）
不可恢复段只在首尾、且每段 < `boundary_window_s`（默认 3s），中部干净（或仅含可恢复段）。

1. **先插值**：若中部仍有可恢复段（`middle_recoverable`），按第 2 步修复；首尾被裁掉的可恢复段无需处理。
2. **再裁剪**：把首尾不可恢复段（外加 `crop_margin_frames` 余量）切掉，得到保留帧窗口
   `[keep_start, keep_end)`。
3. **按帧索引对齐裁剪所有模态**：动作 / 状态 / gripper / raw_gripper 的 `data.csv`，
   以及**每路视频 `video.mp4` 与其 `timestamps.csv`**，统统裁到同一帧窗口——
   “裁剪指视频、gripper、action 等所有数据都按 timestamp 裁剪”。视频用 ffmpeg 逐帧 `select`
   精确裁剪并重编码（帧数与 CSV 行数严格一致）。
4. **重置 timestamp**：每路保留流的 `timestamp_ms` 减去其首个保留值，使裁剪后的新数据**从 0 开始**，
   各流帧数相同、保持对齐。
5. 若裁剪后剩余帧数 < `min_kept_frames`（默认 30），判为太短不可用，不输出。

### 4. 中部不可恢复 → 拒绝（reject）
中部存在不可恢复段（或首尾不可恢复段 ≥ 3s）。数据无法靠平滑/裁剪挽救，**标注 `quality=unusable`、
不输出任何 episode 数据**，仅在报告与 summary 中登记。

## metadata 记录（每条数据都写）

每个**被输出**的 episode，其 `metadata.json` 会：
- 更新 `total_frames` / `duration_seconds` 及各 `modalities` 的 `frames` / `rows`（反映裁剪后帧数）；
- 追加 `preprocessing` 溯源块，记录：判定的**数据类型** `data_type`(+中文)、`category`、
  **质量** `quality`（`good` / `repaired` / `unusable`）、所做**操作** `operations`、
  每侧插值帧数 `interpolated_frames`、裁剪明细 `crop`、原始/保留帧数、所用 `smooth_config`、分类依据。

`meta/episode.json` 的帧数同步更新。**裁剪或修改后会重算** `checksums.sha256` 与
`.checksum_manifest`，使其与实际写出的字节一致。

## 使用方法

```bash
# 单个 episode
python3 preprocess_trajectory.py /path/to/class_name/episode_0001 -o preprocessed

# 批量（类别目录），镜像输入布局写到 -o 根目录下
python3 preprocess_trajectory.py -i /path/to/class_name -o preprocessed

# 任意上层目录递归发现所有 episode_XXX
python3 preprocess_trajectory.py /path/to/dataset_root -o preprocessed

# 只评估+报告、不写数据（看每条会被怎么处理）
python3 preprocess_trajectory.py /path/to/class_name --dry-run
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 输出根目录（默认 `preprocessed`），镜像输入布局。 |
| `--overwrite` | 若输出 episode 目录已存在则覆盖。 |
| `--dry-run` | 只判定与报告，不写任何文件。 |
| `--no-video` | 跳过视频裁剪/拷贝（仅 CSV+metadata，测试用更快）；裁剪类输出将丢弃视频。 |
| `--json PATH` | 额外把汇总写到指定路径（单组扁平、多组包成 `groups`）。 |
| `--config PATH` | 预处理配置（默认同目录 `preprocess_config.json`）。 |
| `--smooth-config PATH` | 突变检测阈值配置（默认 `smooth_assessment_config.json`）。 |
| `--fps` | 覆盖帧率（默认取 `metadata.json` 的 `fps_config`，否则 30）。 |

### 配置 `preprocess_config.json`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `min_kept_frames` | 30 | 裁剪后少于此帧数则判不可用、不输出。 |
| `crop_margin_frames` | 2 | 裁掉不可恢复段时额外多切的余量帧（避免把突变沉降尾巴留在新边界）。 |
| `video_codec` / `video_crf` / `video_preset` | libx264 / 18 / medium | 裁剪类视频重编码参数。 |

> 突变检测阈值（`jump_displacement_m` / `window_s` / `recover_window_s` /
> `return_tolerance_m` / `merge_gap_s` / `boundary_window_s` /
> `boundary_max_unrecoverable_s`）来自 `smooth_assessment_config.json`，含义见同目录
> `README.md`（`smooth_assessment` 的说明）。

## 输出布局

```
preprocessed/<class_name>/
├── episode_0001/                    # 被输出的 episode（passthrough/interpolate/crop）
│   ├── actions.eef_pose/data.csv    # 修复/裁剪后的数据
│   ├── observation.image.*/         # 视频 + timestamps（裁剪类已按帧对齐裁剪）
│   ├── observation.state.*/ ...
│   ├── metadata.json                # 含 preprocessing 溯源块、更新后的帧数
│   ├── meta/episode.json
│   └── checksums.sha256 / .checksum_manifest   # 重算
├── episode_0001.preprocess.json     # 每个 episode 一份处理报告
├── episode_0003.preprocess.json     # （reject 的 episode 只有报告、无数据目录）
└── summary.preprocess.json          # 该组：类别/标签计数 + 每条 episode 的处理明细
```

控制台逐 episode 打印：`episode → category (quality) [operations]`，裁剪类附
`crop[start:end] kept=N`、插值类附 `interp L=.. R=..`，并以
`Summary: processed=… written=… rejected=… errors=… | <类别计数>` 收尾。

## 依赖

- Python 3，`numpy` 与 `scipy`（`scipy.interpolate.PchipInterpolator` 用于插值）。
- `ffmpeg` / `ffprobe`（仅裁剪类需要，用于逐帧裁剪视频）。
- 同仓 `assessment/validate_raw_data.py` 与 `preprocess/smooth_assessment.py`
  （脚本自动按相对路径导入，复用路径发现、IO、突变检测与五类分类）。
