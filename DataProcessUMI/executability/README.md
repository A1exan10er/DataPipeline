# executability —— 采集 TCP 轨迹在各本体上的可执行性求解 + 可执行中段定位

判定一段**采集到的 TCP（末端）轨迹**能否在 `../resources` 中注册的**每一个机型本体**上执行，
并定位其中**连续可执行的中段**（轨迹前后常因够不到 / 姿态越界而不可执行，只有中部可解）。
复用 `../solve` 的求解核心（Pinocchio CLIK 逆解 + Coal 自碰撞），在外面套三层：

1. **输入适配** —— 从 episode 读取 eef_pose 轨迹（6D 旋转表示）；默认先套用 **transform 管线**
   （tracker → world EEF），与 replay 实际消费的帧一致。
2. **平移搜索** —— 只允许整条轨迹做 **xyz 整体平移**（姿态不变），搜索一个平移 `t*`，使其落入
   本体工作空间、且**最长一段连续可执行轨迹**尽可能长。
3. **中段定位** —— 在 `t*` 处全分辨率校验，分别用**严格**与**贴近 replay** 两套阈值，
   找出连续无间断的可执行中段 `[executable_frame_start, executable_frame_end]`。

> 多臂数据解算到单臂本体：默认 `--arm both`，**左右臂分别求解**。

## 依赖
与 solve 相同：`pip install pin scipy`，并确认 `../resources/.ament/install/share` 软链接在。

## 输入数据格式
episode（`~/data/data_samples/<task>/<episode>/`）下：
- `actions.eef_pose/data.csv` —— 动作目标位姿（默认源）
- `observation.state.eef_pose/data.csv` —— 观测实际位姿

CSV 列：`timestamp_ms, left_x left_y left_z left_r1..left_r6 left_gripper, right_...`。
位置米；姿态为 **6D 旋转表示**（旋转矩阵前两列，Gram-Schmidt 还原，见 `read_episode.py`）。

**transform**：原始 data_samples 是 tracker 帧，需先过 transform 管线变到 world EEF 帧才能解算。
本工具默认 `--transform` 开启，读取时在内存中套用 `../transform/ee_transform.py`（与 replay 完全一致），
所以可直接指向**未变换的原始 episode**。若指向 transform 管线的输出（已变换），请加 `--no-transform`
避免二次变换。

## 用法

### 通过主 pipeline 调用（推荐）

```bash
python3 ../pipeline/run_pipeline.py /path/to/data -o pipeline_out \
    --run-executability --ik-robots flexiv_rizon4 ur5e --ik-arm both --ik-jobs 8
```

主 pipeline 会先执行 assessment / preprocess / transform，再对已变换的
`data/<class>_w_world_base/episode_XXXX` 调用本工具，并自动传入 `--no-transform`。
结果写在 `report/<class>/episode_XXXX/executability/`，同时嵌入合并报告的
`executability` 字段。

### 独立运行

```bash
# 默认：双臂、对所有注册本体、套 transform、抽稀到 <=200 点
python solve_executability.py --episode ~/data/data_samples/.../episode_0000

# 指定本体 / 单臂 / 已变换数据 / 并行
python solve_executability.py --episode <ep> --robots flexiv_rizon4 --arm left \
    --no-transform --jobs 8
```

支持的本体（`--robots`，默认全部）：见 `../solve/robots.py:REGISTRY`
（`franka_fr3v2`、`ur5e`、`ur7e`、`flexiv_rizon4`、`aloha_piper`、`arx5_x5`）。

## 输出
输出目录 `out_exec/<arm>/<robot>/`：
| 文件 | 内容 |
|---|---|
| `tcp_shifted.csv` | **平移后的 TCP 轨迹**（`x y z qx qy qz qw t frame executable`，与 transform 轨迹是常量 xyz 偏移） |
| `joints.csv` | **关节轨迹**（逐点关节角 + `ik_ok/pos_err_mm/rot_err_deg`） |
| `report.strict.csv` / `report.replay.csv` | 两套阈值下逐点指标（IK 残差/限位/奇异/碰撞/速度 + `executable/reason`） |
| `placement.json` | 该本体的完整结果（见下） |

跨本体/双臂汇总写在 `out_exec/summary.json`。终端按 `[arm/robot]` 打印结论 + 中段 + 误差。
**退出码**：至少一个 (arm,robot) 可执行=0，否则=1。

### `placement.json` 四项核心结果
1. **能否执行的结论** —— `executable`（replay 判据下存在长度 ≥ `--min-segment` 的连续可执行中段即为 True）。
2. **平移后的 TCP 轨迹** —— `found_offset`（常量 xyz 偏移 `t*`）；逐点轨迹见 `tcp_shifted.csv`
   （= transform 轨迹 + `t*`，姿态不变）。
3. **可执行中段**（`strict` 与 `replay` 各一份）：
   - `executable_frame_start` / `executable_frame_end` —— 连续无间断中段的**原始帧号**；
   - `executable_index_start/end`（抽稀点下标）、`executable_time_start_s/end_s`（时间）、`segment_len`。
4. **求解参数 / error**（`strict`、`replay` 各一份）：`n_executable`、`executable_ratio`、`failure_reasons`，
   以及 `error{ max_pos_err_mm, max_rot_err_deg, min_clearance_mm, min_sigma,
   segment_max_pos_err_mm, segment_max_rot_err_deg }`（`segment_*` 仅统计中段内）。

### 两套阈值（`--min-segment` 控制结论所需中段长度）
- **strict** —— solve 默认（1mm/0.5°、`sigma_min` 0.02、`clearance` 2mm），最保守。
- **replay** —— 贴近真实机械臂 replay 容差（~5mm/~3°、放宽奇异/碰撞余量），更能反映
  「机械臂实际能否执行」，可执行中段通常更长。结论 `executable` 取 replay 判据。

Stage A 即判不可行时给 `overflow_m`（轨迹比工作空间宽多少米），无需求解。

## 关于 arx5 / UMI 工具帧（已知项）
`solve/robots.py` 的 `arx5_x5` 用 `arx5-sdk/models/X5.urdf`（工具长 0.145m）；而 UMI 数据
（如 `*_umi`）对应的是 `X5_umi.urdf`（UMI finray 夹爪，工具长 0.225m，差 80mm，沿工具轴）。
若解算 arx UMI 数据，本体的 TCP 工具帧应与采集时一致，否则可达性会系统性偏差。
（验证已优先在 flexiv 上完成；arx 工具帧对齐作为单独项。）

## 与 solve 的关系
求解逻辑全部 `import` 自 `../solve`：`fit_trajectory`（平移搜索）、`batch.validate`（全校验）、
`core`（IK+指标）、`check_trajectory.write_report`、`workspace_bounds`。本目录新增：
- `read_episode.py` —— episode eef_pose（6D 旋转，可选 transform）→ `(List[SE3], times, frames)`。
- `solve_executability.py` —— 遍历本体/手臂的批量驱动 + 中段定位 + 双阈值输出。

## 文件结构
```
executability/
├── read_episode.py          # eef_pose 读取（6D 旋转 -> SE3，可选 transform，返回原始帧号）
├── solve_executability.py   # 批量驱动：平移搜索 + 双阈值全校验 + 可执行中段定位 + 输出
└── README.md
```
