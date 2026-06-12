# 轨迹平滑评估（preprocess / smoothing）

`assessment` 只判断采集数据是否**有效**；本目录在其基础上进一步判断 `actions.eef_pose`
轨迹是否**需要平滑、能否平滑**。它检测末端位姿 x/y/z 的**突变**（jump），把每一帧——
**左、右设备分别**——判定为三种状态之一，再据此给整段轨迹打出五类标签之一，并对每个
episode 输出一份结构化 JSON 报告。

## 目录文件

| 文件 | 作用 |
| --- | --- |
| `smooth_assessment.py` | **主入口**。检测突变、分段、分类，输出报告与控制台摘要。 |
| `smooth_assessment_config.json` | 阈值配置（默认读取此文件，可被命令行覆盖）。 |

> 输入路径形式、输出目录镜像布局，与 `assessment/validate_raw_data.py` **完全一致**
> （脚本直接复用其 `find_episode_dirs` / `episode_output_layout` / IO 辅助函数）。

## 检测模型（逐设备，仅 x/y/z）

记 `P[i]` 为第 i 帧的位置，`window_s` 默认 0.5s，`jump_displacement_m` 默认 0.35m。

1. **窗口位移** `rdisp[i] = ||P[i] - P[k]||`，其中 `k` 是仍落在 `[t[i]-window_s, t[i]]`
   内的最早一帧（即“过去 0.5s 内移动了多远”）。`rdisp[i] > 0.35m` 的帧记为 *fast*（突变帧）。
2. **突变段**：极大连续的 fast 帧段即为一个突变段；间隔短于 `merge_gap_s`（默认 0.5s）的
   相邻 fast 段会被合并，避免一个“出去又回来”的尖峰在顶点被切成两段。
3. **可恢复 vs 不可恢复**：对每个突变段 `[a, b]`，取突变前最后一个平滑位置作为**锚点**
   （`P[a-1]`，段起于第 0 帧时取 `P[a]`）。位置先离开到峰值，若在突变起点后 `recover_window_s`
   （默认 1.0s）内**回到锚点 `return_tolerance_m`（默认 0.35m）以内**，则该段为
   **可平滑突变段** `recoverable`；否则为**不可平滑突变段** `unrecoverable`，该段持续到
   窗口速度重新降到阈值以内（突变段结束）为止。其余帧为**平滑段** `smooth`。

> 锚点判定基准：相对**突变前锚点 ±0.35m**（可配置）。这意味着“瞬时尖峰出去又弹回原处”
> 属可恢复；“瞬移到新位置并停留”因不再回到锚点，属不可恢复（且其不可恢复段长度 ≈ 速度
> 平息所需的约 0.5s）。

每个 episode 报告的 `devices.{left,right}` 下给出：逐帧三态计数、突变事件列表
（`events`：起止帧/时间、峰值位移、锚点、是否回归、回归帧）、以及把逐帧状态做游程编码的
**分段（`segments`，即轨迹中的分点）**。

## 整段轨迹分类（合并左右；“任一设备”即升级）

“首尾边界段”指落在轨迹前 `boundary_window_s`（默认 3s）或后 3s 内的不可恢复段。

| 标签 | code | 条件 |
| --- | --- | --- |
| **平滑轨迹** | `smooth` | 左右设备全为平滑段，无任何突变。 |
| **可恢复轨迹** | `recoverable` | 任一设备仅有可平滑突变段+平滑段，**无任何不可恢复段**。 |
| **中部平滑轨迹** | `middle_smooth` | 不可恢复段**只在首尾**且每段 **< 3s**；中部全部为平滑段。 |
| **中部可恢复轨迹** | `middle_recoverable` | 不可恢复段只在首尾且每段 < 3s；中部存在可平滑突变段+平滑段。 |
| **不可恢复轨迹** | `unrecoverable` | 任一设备在**中部**存在不可恢复段，或首尾不可恢复段 **≥ 3s**。 |

`smooth` 无需平滑、`unrecoverable` 无法靠平滑挽救；其余三类 `smoothable=true`。
报告 `trajectory_label.per_device` 给出每侧把不可恢复段归入“首尾边界 / 中部 / 超长边界”的明细，
便于复核分类依据。

## 使用方法

```bash
# 单个 episode
python3 smooth_assessment.py /path/to/class_name/episode_0001

# 批量（类别目录），并指定输出根目录
python3 smooth_assessment.py -i /path/to/class_name -o outputs

# 任意上层目录递归发现所有 episode_XXX，并镜像输入布局写报告
python3 smooth_assessment.py /path/to/dataset_root -o outputs
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 报告输出根目录（默认 `outputs`）。 |
| `--no-reports` | 只打印控制台摘要，不写 JSON。 |
| `--json PATH` | 额外把汇总写到指定路径（单组扁平、多组包成 `groups`）。 |
| `--config PATH` | 指定阈值配置（默认同目录 `smooth_assessment_config.json`）。 |
| `--fps` | 覆盖帧率（默认取 `metadata.json` 的 `fps_config`，否则 30）。 |
| `--jump-displacement-m` / `--window-s` | 覆盖突变阈值与检测窗口。 |
| `--recover-window-s` / `--return-tolerance-m` | 覆盖回归时限与回归容差。 |
| `--merge-gap-s` | 覆盖 fast 段合并间隔。 |
| `--boundary-window-s` / `--boundary-max-unrecoverable-s` | 覆盖首尾区间与边界段最大时长。 |

## 输出报告

输出布局与 `assessment` 一致：

```
outputs/<class_name>/
├── episode_0001.smoothing.json   # 每个 episode 一份
├── episode_0002.smoothing.json
└── summary.smoothing.json        # 该组所有 episode 的标签汇总
```

每份 episode 报告含：`fps` / `frame_count` / `duration_s`、所用 `config`、`trajectory_label`
（五类标签 + 依据），以及 `devices.left` / `devices.right`（逐帧三态计数、突变 `events`、
分段 `segments`）。控制台同时逐 episode 打印可读摘要，并以
`Summary: processed=… smoothable=… errors=… | smooth=… recoverable=… …` 收尾。

## 依赖

- Python 3（仅标准库；距离计算用 `math`，无需 numpy/opencv）。
- 同仓 `assessment/validate_raw_data.py`（脚本自动按相对路径导入，复用其路径发现与 IO 辅助）。
