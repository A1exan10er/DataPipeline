# 数据质量自动化 QA 方案

最后更新：2026-05-27

相关结构说明：

```text
docs/NAS_SAMPLE_DATA_STRUCTURE.md
```

英文版本：

```text
docs/DATA_QUALITY_AUTOMATION_PLAN.md
```

## 目标

开发一套自动化数据质量检查流程，用于保留高质量机器人和 UMI 采集数据，同时识别、隔离，或在规则充分验证后移除不合格数据。整个过程需要可解释、可追溯、可回滚，并且能够适配不同任务、机器人、相机配置和操作设备。

当前阶段的重点是“检测和报告”，不是自动删除。删除或移动数据之前，必须先在真实样本上验证质量规则是否可靠。

## 基本原则

- 不先删除数据。先生成报告，必要时只做隔离。
- 检测和处理分离。质量检查只输出结构化问题，后续工具再根据报告移动或处理失败样本。
- 优先使用 metadata 和轻量文件检查，避免一开始就读取大量 CSV 或 MP4。
- 每一个质量判断都必须能说明具体原因。
- 阈值尽量按任务、机器人、相机类型分别设置。
- 对异常速度、抖动、关节值、夹爪距离和 episode 时长使用物理意义明确的检查。
- 对用于真实机器人训练的 UMI 数据，需要验证其运动轨迹是否满足目标机器人的逆运动学。
- 边界情况优先标为 `needs_review`，不要强行判定为合格或失败。
- 每个决策都要写入 manifest，便于后续分析直接过滤数据，而不是必须改变原始目录结构。

## 建议状态值

```text
pass
warning
fail
needs_review
```

含义：

- `pass`：没有发现 critical 或 major 问题。
- `warning`：结构上可用，但存在轻微质量问题。
- `fail`：存在关键问题，或多个严重问题，判定为不合格。
- `needs_review`：统计上异常或判断不确定，需要人工复核。

## 建议严重程度

```text
critical
major
minor
info
```

含义：

- `critical`：关键文件缺失或不可读、metadata 无效、载荷文件损坏、同步严重失败等。
- `major`：严重掉帧、大时间戳间隔、行数/帧数明显不一致、机器人状态异常突变等。
- `minor`：轻微 FPS 偏移、轻微时长异常、可选文件缺失、非阻塞 schema 差异等。
- `info`：不影响通过与否，但对统计和总结有用的信息。

## 目标输出

主报告：

```text
quality_report.csv
```

建议字段：

```text
episode_path,task,date,operator,robot,controller,status,severity,reasons,checked_at
```

详细问题报告：

```text
quality_findings.jsonl
```

每一行描述一个具体问题：

```text
episode_path,check_name,severity,status,message,details
```

总结报告：

```text
quality_summary.md
```

总结报告应包含：

- 检查的 episode 总数；
- `pass`、`warning`、`fail`、`needs_review` 数量；
- 按检查项统计的问题数量；
- 按任务统计的问题数量；
- 按操作者统计的问题数量；
- 按机器人和控制设备统计的问题数量；
- 失败和边界样本示例。

## 阶段 1：结构和 Metadata 检查

状态：未开始

目的：在不读取大型视频文件的情况下，先捕获稳定、低成本、可靠的问题。

检查项：

- `metadata.json` 是否存在。
- `metadata.json` 是否为合法 JSON。
- episode 文件夹名是否以 `episode_` 开头。
- 父路径是否符合 `<task>/<date>/<operator>/<episode>`。
- 必要 metadata 字段是否存在：
  - `task_key`；
  - `episode_index`；
  - `duration_seconds`；
  - `total_frames`；
  - `fps_actual` 或 `fps_config`；
  - `modalities`；
  - `quality`。
- `modalities` 是否可读，并且和实际 modality 文件夹对应。
- `.checksum_manifest` 是否存在。
- 必要 modality 文件是否存在：
  - CSV modality：`data.csv`；
  - 图像 modality：`video.mp4` 和 `timestamps.csv`。
- 必要文件是否为空。
- `quality.labels` 是否存在，并可用于质量过滤。

第一个建议工具：

```text
quality_check_episodes.py
```

初始行为：

- 遍历一个或多个任务文件夹；
- 通过 `metadata.json` 发现 episode；
- 执行结构和 metadata 检查；
- 写出 `quality_report.csv`；
- 写出 `quality_findings.jsonl`；
- 写出 `quality_summary.md`；
- 不移动、不删除任何数据。

## 阶段 2：时长和数量一致性检查

状态：未开始

目的：使用 metadata 和轻量 CSV/timestamp 统计识别可疑采集。

检查项：

- `duration_seconds` 是否存在且为正数。
- `total_frames` 是否为正数。
- `duration_seconds`、`total_frames` 和 FPS 是否大致一致。
- episode 时长是否是任务级异常值。
- modality 的行数或帧数是否内部一致。
- 图像 `timestamps.csv` 行数是否接近视频帧数。
- action/state CSV 行数是否接近期望帧数。

推荐统计方法：

- 按 `task_key` 分组；
- 计算中位数和 IQR；
- 时长异常先标为 `needs_review`；
- 只有阈值验证稳定后，才升级为 `fail`。

已有可复用脚本：

```text
check_episode_durations.py
```

潜在增强：

- 将其中的时长读取和按任务异常检测逻辑复用到质量检查工具中。

## 阶段 3：时间戳同步检查

状态：未开始

目的：检测文件存在但不同 modality 不同步，或时间序列损坏的情况。

检查项：

- 时间戳是否单调递增。
- 是否存在重复时间戳。
- `timestamp_ms` 是否存在大间隔。
- 每个 modality 的实际频率是否接近期望 FPS 或控制频率。
- 各 modality 的起止时间是否基本对齐。
- 同时存在 `timestamps_raw.csv` 和 `timestamps.csv` 时，两者差异是否可解释。

注意事项：

- UMI 和真实机器人样本可能有不同相机配置。
- 真实机器人样本可能包含 `third_view`、`second_third_view` 和 flow 视频。
- 检查工具应自动发现 modality，而不是要求固定相机集合。

## 阶段 4：视频健康检查

状态：未开始

目的：检测视频损坏、空白、冻结或与 metadata 不一致的问题。

检查项：

- MP4 是否可以被 `ffprobe`、OpenCV 或其他可靠视频读取器打开。
- 是否能获取视频帧数。
- 视频时长是否大致匹配 metadata 和 timestamps。
- 分辨率是否匹配 metadata 或 `config.csv`。
- 抽样帧是否全黑、全白或为空。
- 抽样帧是否在整个 episode 中冻结不变。
- 后续可以加入模糊度、亮度等检查。

性能原则：

- 默认抽样帧，不完整解码全部视频。
- 只对可疑或指定 episode 进行完整视频解码。

## 阶段 5：机器人状态合理性检查

状态：未开始

目的：检测不可能或损坏的机器人运动数据。

检查项：

- CSV 数值是否能正确解析。
- 是否存在 NaN 或 Inf。
- 时间戳是否单调递增。
- 关节位置是否为有限值。
- 夹爪值是否为有限值。
- 关节值是否在机器人特定关节限制内。
- 夹爪距离是否在机器人特定限制内。
- episode 时长是否在任务期望范围内。
- 速度是否为有限值，并在机器人速度限制内。
- 由位置估算出的加速度和 jerk 是否异常。
- 相邻帧关节跳变是否低于机器人阈值。
- 相邻帧夹爪跳变是否低于机器人阈值。
- 末端执行器位姿跳变是否低于机器人阈值。
- 抖动分数是否低于任务和机器人阈值。
- 运动是否异常静止，除非任务允许静止阶段。

单位：

- 真实机器人关节位置单位为弧度；
- 真实机器人夹爪距离或位置单位为米；
- 阈值应尽量按机器人单独配置。

机器人列名模式：

- ARX 双臂样本使用 `left_j*`、`right_j*`、`left_gripper`、`right_gripper`。
- Flexiv 样本使用 `j1`-`j7`、`joint_*.pos` 和 `gripper`。

推荐异常值方法：

- 从时间戳计算 `dt`，不要假设固定帧率；
- 没有速度流时，从相邻位置计算速度；
- 从速度计算加速度；
- 必要时从加速度计算 jerk；
- 使用中值滤波或 Hampel filter 检测孤立尖峰，同时不掩盖持续异常运动；
- 同时报告最大值和稳健分位数，如 p95、p99、p99.9；
- 对不可能值直接 fail，例如 NaN、Inf、超过物理硬限制、巨大不连续跳变；
- 对统计上异常但不明显不可能的情况标为 `needs_review`。

推荐抖动检查：

- 将原始运动与平滑轨迹比较，检测高频位置噪声；
- 计算每个关节平滑后的残差；
- 如果已有 FK，可计算末端执行器残差；
- 先使用任务相对阈值，后续在有足够标注样本后改进为机器人特定阈值。

建议输出字段：

```text
max_joint_abs,max_joint_step,max_velocity,max_acceleration,max_jerk,jitter_score,static_ratio
```

## 阶段 6：逆运动学兼容性检查

状态：未开始

目的：过滤无法被目标真实机器人执行的 UMI episode。UMI 数据即使结构上有效，如果其末端执行器运动无法在目标机器人上求解逆运动学，就不适合用于训练控制该机器人模型。

目标机器人：

```text
UR
ARX5
Flexiv
Aloha
Piper
Franka
```

核心思路：

- 将 UMI 记录的末端执行器轨迹作为期望任务空间轨迹。
- 对每个目标机器人加载正确的机器人模型、关节限制、夹爪限制、base frame、tool frame 和 IK solver。
- 沿整个轨迹求解 IK。
- 检查是否存在连续、满足关节限制、至少自洽的解；后续可加入碰撞检查。
- 只有通过目标机器人 IK 兼容性检查的 episode，才标记为该机器人的 training-ready。

推荐实现阶段：

1. 机器人模型注册表

为每个目标机器人建立版本化配置：

```text
robot_id
urdf_or_description_path
base_frame
tool_frame
joint_names
joint_limits
velocity_limits
acceleration_limits
gripper_limits
ik_solver
solver_parameters
```

不要把机器人限制硬编码在检查器里。应放在配置文件中，便于审核和更新。

2. 运动学后端选择

不要从零实现 IK，应使用成熟机器人运动学库或已有 SDK。可选方案：

- Pinocchio：用于 FK、Jacobian 和模型检查；
- IKPy 或 TRAC-IK 风格 solver：用于数值 IK；
- 机器人厂商 SDK 或内部 solver：如果可靠且可复现；
- MoveIt 风格 IK 和规划检查：如果本地有 ROS/ROS2 环境。

质量检查器应在后端外包一层简单接口：

```text
solve_ik(robot_id, target_pose, seed_joint_state) -> ik_result
forward_kinematics(robot_id, joint_state) -> pose
check_joint_limits(robot_id, joint_state) -> result
```

3. 坐标系标定和变换处理

只有坐标系正确，IK 验证才有意义。需要明确以下变换：

```text
UMI/task frame -> robot base frame
UMI gripper/tool frame -> robot tool frame
camera/world frame -> robot base frame, if pose is camera-derived
```

这些变换应按任务、实验场景或机器人工作站保存。缺失或不确定的变换应输出 `needs_review`，不要直接 fail。

4. 轨迹级 IK，而不是单点 IK

单帧 IK 成功不代表整段轨迹可执行。需要检查连续运动：

- 第 0 帧使用中性位姿或配置好的 seed；
- 第 N 帧使用第 N-1 帧关节解作为 seed；
- 优先选择最接近上一帧关节状态的解；
- 拒绝大的关节不连续；
- 拒绝超出关节限制的解；
- 拒绝超过速度和加速度限制的解；
- 记录整个 episode 的 IK 失败比例。

建议指标：

```text
ik_success_ratio
ik_max_position_error
ik_max_rotation_error
ik_mean_position_error
ik_joint_limit_violation_count
ik_max_joint_step
ik_max_joint_velocity
ik_max_joint_acceleration
ik_failure_segments
```

5. 可达性预检查

在运行完整 IK 前，先做低成本可达性检查：

- 目标位置到机器人 base 的距离是否在大致工作空间半径内；
- 目标高度是否在合理工作空间范围内；
- 目标姿态是否明显不可能；
- 轨迹是否长期贴近工作空间边界。

这不能替代 IK，但可以快速识别明显不可能的 episode，并降低计算成本。

6. 碰撞和环境检查

初期可以在环境几何缺失时先不做环境碰撞检查。但最终高可信过滤应加入：

- 自碰撞检查；
- 桌面或工作空间碰撞检查；
- 工具和夹爪碰撞检查；
- 已知任务障碍物检查。

在碰撞检查实现之前，通过 IK 的 episode 应标记为 `ik_reachable_no_collision_check`，不要直接声称完全可执行。

7. 按机器人记录训练可用性

同一个 episode 可能适合某个机器人，但不适合另一个机器人。应按目标机器人分别记录：

```text
episode_path,target_robot,ik_status,ik_reasons,ik_metrics
```

建议 IK 状态：

```text
ik_pass
ik_warning
ik_fail
ik_needs_review
ik_not_applicable
```

第一版推荐判定规则：

- `ik_fail`：连续较长片段无法得到合法 IK 解。
- `ik_fail`：IK 解违反关节限制。
- `ik_fail`：所需关节速度或加速度超过机器人硬限制。
- `ik_needs_review`：缺少或不确定标定变换。
- `ik_warning`：IK 成功，但长期接近关节限制或工作空间边界。
- `ik_pass`：IK 连续成功，位姿误差可接受，关节运动平滑。

8. UMI 到真实机器人的映射验证

UMI 夹爪运动不一定能一对一映射到每种真实机器人夹爪。需要验证：

- 左/右手映射；
- 单臂和双臂映射；
- 夹爪开合范围转换；
- TCP/tool center point 约定；
- 任务是否需要单臂或双臂。

如果映射关系未知，应输出 `ik_needs_review`。

建议第一个 IK 工具：

```text
quality_check_ik.py
```

初始行为：

- 读取结构质量报告；
- 选择已通过结构和运动检查的 UMI episode；
- 加载目标机器人配置；
- 执行可达性预检查；
- 先对抽样帧运行轨迹 IK；
- 对候选 episode 可选运行全帧 IK；
- 写出 `ik_quality_report.csv`；
- 写出 `ik_quality_findings.jsonl`；
- 不移动、不删除数据。

实际推进方式：

- 先选择一个模型和 IK solver 最可靠的目标机器人；
- 用已知好样本和坏样本验证检查器；
- 再逐步加入 UR、ARX5、Flexiv、Aloha、Piper 和 Franka；
- 每个机器人的阈值单独维护。

## 阶段 7：综合质量判定

状态：未开始

目的：整合结构、时间戳、视频、机器人状态、异常值和 IK 结果，形成最终质量结论。

建议逻辑：

- 结构 `critical` 问题 -> `fail`；
- 必要视频或 CSV 不可读 -> `fail`；
- 不可能的异常值 -> `fail`；
- 严重时间同步损坏 -> `fail`；
- 任务级时长异常 -> `needs_review`，除非明显不可能；
- UMI episode 未通过目标机器人 IK -> 对该机器人不是 training-ready；
- 一个机器人 IK 通过，不代表所有机器人 IK 通过；
- warning 不应触发自动删除。

建议最终训练可用性输出：

```text
episode_path,general_quality_status,training_ready,target_robot,ik_status,reasons
```

## 阶段 8：人工复核和隔离流程

状态：未开始

目的：在不可逆删除之前，把确认不合格数据和可用数据分离。

建议隔离目录：

```text
<task>/_quarantine/<date>/<operator>/<episode>
```

建议工具：

```text
quality_quarantine.py
```

行为：

- 读取 `quality_report.csv`；
- 只移动 `status=fail` 的 episode；
- 默认跳过 `needs_review`；
- 移动前写出 move manifest；
- 默认 dry-run；
- 永不永久删除。

建议 move manifest：

```text
original_path,new_path,status,reasons,moved_at
```

## 阶段 9：人工复核支持

状态：未开始

目的：让边界样本的人工判断更高效、更可追溯。

建议复核材料：

- 关键相机抽样缩略图；
- 可选短视频预览；
- CSV 统计摘要；
- 时长和掉帧摘要；
- 每个 episode 的主要问题；
- 建议状态和原因。

该阶段重点处理 `needs_review` 和高影响 `warning` episode。

## 当前实现进度

已完成：

- 已在 `docs/NAS_SAMPLE_DATA_STRUCTURE.md` 中记录 NAS 样本基本结构。
- 已记录当前 task-folder 布局、modality、相机、文件格式和 CSV header。
- 已记录 UMI 和真实机器人相机配置差异。
- 已记录真实机器人操作设备和单位说明。
- 已创建英文版质量自动化方案。
- 已加入异常速度、抖动、关节、夹爪和 episode 时长过滤要求。
- 已将 UMI 到真实机器人的逆运动学验证作为独立阶段加入方案。
- 已记录目标 IK 机器人系列：UR、ARX5、Flexiv、Aloha、Piper、Franka。
- 已创建本文档，用于 QA 全流程中文说明。

未开始：

- 尚未实现自动质量检查器。
- 尚未由代码生成报告 schema。
- 尚未实现 quarantine 或删除行为。
- 尚未验证任何阈值。
- 尚未创建机器人模型注册表。
- 尚未选择或集成 IK 后端。
- 尚未提供 UMI 到目标机器人坐标系的标定变换。
- 尚未验证机器人特定 IK 阈值。

下一步实现：

```text
Implement Phase 1 in quality_check_episodes.py with dry-run report-only behavior.
```

IK 并行准备：

```text
Collect robot models, joint limits, gripper limits, tool frames, base frames, and UMI-to-robot calibration transforms for UR, ARX5, Flexiv, Aloha, Piper, and Franka.
```

## 待确认问题

- 哪些 `quality.labels` 应被视为 pass、warning、fail 或 needs-review？
- 任务级时长异常应先标为 `needs_review` 还是直接 `fail`？
- 每种相机类型可接受的掉帧阈值是多少？
- UMI、ARX、Flexiv、UR 的时间戳间隔阈值是多少？
- 每种机器人的关节和夹爪限制应使用哪份配置？
- `_quarantine` 应放在每个 task 文件夹内，还是放在单独的顶层 quarantine root？
- 第一个 IK 实现目标应选择哪种机器人？
- 本地环境优先使用哪个运动学后端：Pinocchio、IKPy、TRAC-IK、MoveIt、厂商 SDK，还是内部 solver？
- UR、ARX5、Flexiv、Aloha、Piper、Franka 的 URDF 或机器人描述文件在哪里？
- 每个机器人的 base frame 和 tool frame 名称是什么？
- 哪些变换用于把 UMI task-space pose 映射到目标机器人 base frame？
- UMI 左/右夹爪位姿如何映射到单臂机器人？
- IK 验证应使用每一帧，还是先使用抽样轨迹？
- 用于 training eligibility 的位姿误差容忍度是多少？
- 在认为数据 training-ready 之前，是否必须完成碰撞检查，还是第一阶段只检查可达性和关节可行性？
