# solve —— TCP 轨迹求解与可执行性校验

基于 **Pinocchio**（运动学/雅可比）+ **Coal**（自碰撞、带符号距离）的两个命令行程序，
不依赖 ROS。机型 URDF/碰撞模型来自同级目录 `../resources`（见其 `README.md`）。

- **`check_trajectory.py`** —— 判定一段 TCP 轨迹**是否可执行**（逐点报告 + 汇总）。
- **`tcp_to_joints.py`** —— 把 TCP 轨迹**解算成关节序列**（逐点关节角）。

两者共用同一套求解核心，区别只在输出。

## 依赖

```bash
pip install pin            # 含 pinocchio + coal
```
首次需确保 `../resources/.ament/install/share` 软链接存在（用于 `package://` 网格解析），
`../resources/README.md` 有说明。

## 支持的机械臂

| `--robot` | DOF | TCP frame | 夹爪关节(锁定) | SRDF |
|---|---|---|---|---|
| `franka_fr3v2` | 7 | `fr3v2_hand_tcp` | finger_joint1/2 | ✅ |
| `ur5e` | 6 | `tool0` | — | 相邻自动屏蔽 |
| `ur7e` | 6 | `tool0` | — | 相邻自动屏蔽 |
| `flexiv_rizon4` | 7 | `flange` | — | 相邻自动屏蔽 |
| `aloha_piper` | 6 | `gripper_base` | joint7/8 | ✅ |
| `arx5_x5` | 6 | `eef_link` | — | 相邻自动屏蔽 |

> 注册表见 `robots.py:REGISTRY`。新增机型只需加一行（URDF 路径 + TCP frame + 夹爪关节）。

---

## 输入格式（两个程序通用）

CSV，每行一个 TCP 位姿。`#` 开头为注释。有表头则按列名取列，否则按固定顺序。

- `--rot quat`（默认）：列 `x y z qx qy qz qw`（位置 m，四元数）
- `--rot rpy`：列 `x y z roll pitch yaw`（位置 m，欧拉角 rad，固定轴 XYZ）
- 可选时间列：列名 `t`，或用 `--time-col 列名`。**有时间列才会做关节速度校验。**

位姿默认表示 **TCP frame 相对 base/world** 的目标位姿。

生成样例：`python examples/make_example.py --robot ur5e --n 40 --out examples/ur5e_traj.csv`

---

## 程序一：`check_trajectory.py`（可执行性判定）

```bash
python check_trajectory.py --robot ur5e --input traj.csv --rot quat \
       --time-col t --out report.csv
# 大批量 + 无 SRDF 机型建议开碰撞标定与并行：
python check_trajectory.py --robot flexiv_rizon4 --input traj.csv \
       --jobs 8 --calibrate 200 --out report.csv
```

**输出**
- `report.csv`：逐点指标（见下）+ `reason`（失败原因）。
- `<out>.summary.json`：可执行比例、首个失败点、失败原因分布、最差/平均质量、最大误差等。
- 终端打印汇总。**退出码**：全部可执行=0，否则=1（便于流水线判定）。

**逐点列含义**

| 列 | 含义 |
|---|---|
| `ik_ok` | IK 是否收敛到目标位姿 |
| `pos_err_mm` / `rot_err_deg` | IK 位姿残差（**误差估计**） |
| `in_limits` / `limit_margin_rad` | 关节是否越限 / 最小余量 |
| `sigma_min` / `cond` / `manip` | 雅可比最小奇异值 / 条件数 / 可操作度（**奇异度/质量**） |
| `self_collision` / `clearance_mm` | 是否自碰撞 / 最近带符号距离（>0 安全余量，<0 穿透深度） |
| `vel_ratio` | 相邻点最大 `|dq/dt| / 速度限位`（无时间列为 `nan`） |
| `executable` | 综合判定（下述全满足） |
| `quality` | 0~1 质量分（各项归一化取最小，短板决定） |

**`executable = True` 当且仅当**：IK 收敛 ∧ 不越限 ∧ 无自碰撞 ∧ `sigma_min ≥ 阈值` ∧ `clearance ≥ 阈值` ∧ 速度不超限。

**阈值参数**（默认值）：`--pos-tol-mm 1.0` `--rot-tol-deg 0.5` `--sigma-min 0.02`
`--clearance-mm 2.0` `--vel-ratio 1.0`。

---

## 程序二：`tcp_to_joints.py`（TCP → 关节序列）

```bash
python tcp_to_joints.py --robot ur5e --input traj.csv --rot quat --out joints.csv
python tcp_to_joints.py --robot aloha_piper --input traj.csv --deg --only-reachable --jobs 4
```

**输出** `joints.csv`，列：`index, t, <各关节名>, ik_ok, pos_err_mm, rot_err_deg`
- 关节角默认 **弧度**，`--deg` 改角度。
- 默认输出全部点；不收敛点给出的是**最优近似解**（残差见 `pos_err_mm`）。
- `--only-reachable` 只保留 IK 收敛的点。
- 退出码：全部收敛=0，否则=1。

---

## 内部逻辑

### 1. 模型加载（`robots.py`）
1. `buildModelFromUrdf` + `buildGeomFromUrdf`（COLLISION）。`package://` 经
   `../resources/.ament/install/share` 解析（含 `ur_description→universal_robots` 等软链接）。
2. **锁定夹爪关节**：`buildReducedModel` 把夹爪关节固定，得到只含手臂自由度的模型。
   这样雅可比不含夹爪的零列，**条件数/奇异值才有物理意义**，IK 也不会浪费自由度。
3. **碰撞对清理**（避免相邻连杆几何重叠造成的假阳性）：
   - 全部碰撞对 → 去掉 SRDF 中 `disable_collisions` 的对（若该机型有 SRDF）；
   - 去掉**相邻**（父子/同关节）连杆对；
   - `--calibrate N`：随机采样 N 个构型，去掉**恒碰撞**（>95% 命中）的对——
     给无 SRDF 的机型（UR/Flexiv/ARX5）补这一步更稳。

### 2. 逆运动学（`core.py:solve_ik`）
阻尼最小二乘 CLIK（闭环逆运动学）：

```
迭代: e = log6( T_cur⁻¹ · T_target )          # 6D 位姿误差
      J = -Jlog6(iMd⁻¹) · FrameJacobian       # 误差对 q 的雅可比
      v = -Jᵀ (J Jᵀ + λI)⁻¹ e                 # 阻尼最小二乘步
      q = clip( integrate(q, v), 限位 )
直到 ‖e_pos‖<pos_tol 且 ‖e_rot‖<rot_tol，或达到 max_iters
```
- **热启动**：沿轨迹用上一点的解作为下一点种子，保证关节序列**连续**、收敛快。
- **随机重启**：每段段首冷启动失败时，在限位内随机取若干种子重试（`--restarts`）。
- 残差即 `pos_err_mm / rot_err_deg`，是该点的**误差估计/质量依据**。

### 3. 单点指标（`core.py`）
- `jacobian_metrics`：对 TCP frame 雅可比做 SVD → `sigma_min`（距奇异远近）、`cond`、可操作度。
- `joint_limit_check`：`q` 对上下限的最小余量。
- `self_collision`：`computeCollisions`（布尔）+ `computeDistances`（最近带符号距离）。
- `quality`：各项归一化后取最小值（短板决定整体质量）。

### 4. 批量与并行（`batch.py`）
- 默认单进程，沿轨迹热启动（构型连续性最好）。
- `--jobs N`：把轨迹切成 N 段**连续块**，多进程并行；块内热启动，**块界冷启动**（带随机重启）
  保证每块独立正确。块界处构型连续性可能略弱，但吞吐量随核数近线性提升。
- 速度校验（`add_velocity_checks`）在每块内用 `pin.difference` 差分相邻关节并除以 `dt`。

### 5. 时间复杂度
单点 IK 约几十次迭代、毫秒级；指标为常数级。整体 ≈ O(点数 × 迭代数)，
十万级轨迹配合 `--jobs` 可在分钟级完成。

---

## 文件结构

```
solve/
├── check_trajectory.py   # 程序一：可执行性判定（CLI + 报告/汇总）
├── tcp_to_joints.py      # 程序二：TCP → 关节序列（CLI）
├── fit_trajectory.py     # 程序三：找可行整体平移 + 求解 + 可视化（CLI）
├── workspace_bounds.py   # 工作空间包围盒估计（FK 采样，含台面安装约束）
├── ik_pink.py            # 交叉验证（独立双解）+ 可选 pink(QP-IK)
├── viz_meshcat.py        # MeshCat 关节轨迹动画（导出自包含 HTML）
├── batch.py              # 批量驱动：热启动 + 多进程连续分块
├── core.py               # IK 求解器 + 单点指标 + 质量分
├── robots.py             # 机型注册表 + 模型加载（锁夹爪 + 碰撞对清理）
├── io_poses.py           # TCP 位姿 CSV 读取（quat/rpy + 时间列）
├── workspace_configs/    # 各机型 xyz 工作空间 config（workspace_bounds 生成）
├── examples/
│   ├── make_example.py   # 生成可达样例轨迹（关节摆动 FK）
│   ├── ur5e_traj.csv     # 样例
│   └── oor_arc.csv       # 越界样例（用于演示程序三的平移搜索）
└── README.md
```

## 程序三：为轨迹寻找可行整体平移（fit_trajectory.py）

只允许整条轨迹做 **xyz 整体平移**（姿态不变），找到一个使其可执行的摆放即可。
未知量仅 `t∈R³`，分阶段由廉价到昂贵求解：

1. **Stage A 几何裁剪**：轨迹位置盒须整体落入机器人（台面安装）工作空间盒，
   得到闭式候选平移盒 `t∈[Wmin−Bmin, Wmax−Bmax]`；某轴为空即判不可行（给出溢出量）。
2. **Stage B 灵巧中心播种**：`t0 = 可达点云形心 − 轨迹形心`。
3. **Stage C 3 自由度搜索**：候选盒内对最难路点算罚函数（IK 残差/越限/碰撞/奇异），
   粗撒点 + Nelder-Mead 精修，`f=0` 即全可行，找到一个就早停。
4. **Stage D 全分辨率校验**：对 `t*` 跑 `batch.validate`（含速度/连续性等全部检查），
   写出 `placement.json` / `report.csv` / `joints.csv`，可选交叉验证与 MeshCat 动画。

```bash
# 把越界轨迹平移到 ur5e 可执行处，输出关节轨迹 + 交叉验证 + 动画
python fit_trajectory.py --robot ur5e --input examples/oor_arc.csv --rot quat \
    --time-col t --outdir out_fit --cross-check --viz

# 用完整几何外包络（不套台面约束）；或自定义台面 floor
python fit_trajectory.py --robot flexiv_rizon4 --input traj.csv --free-space
python fit_trajectory.py --robot aloha_piper  --input traj.csv --z-floor -0.1
```

输出 `out_fit/`：`placement.json`（t*、可行判定、候选盒、误差汇总、失败点）、
`report.csv`（逐点误差/指标）、`joints.csv`（关节轨迹）、`cross_check.json`、
`anim.html`（浏览器打开即播放机械臂动画，叠加目标路径与工作空间盒）。

> 额外依赖：`scipy`（搜索）、`meshcat`（可视化）、可选 `pin-pink`+`quadprog`（pink 交叉验证）。
> 速度类失败属轨迹**时序**问题（与摆放弱相关），可放宽 `--vel-ratio` 或对轨迹重定时。

## 常见提示
- 多款机型的“限位中点”恰好接近**奇异位形**（如 UR 的 q=0 是手臂完全伸展），
  样例轨迹绕其摆动时会触发 `near_singular`——这是正常的奇异检测，而非 IK 失败。
- 若只关心“能否到达”而不在意奇异/碰撞余量，调小 `--sigma-min`、`--clearance-mm` 即可。
- 位姿目标是 **TCP frame**；换参考点请在注册表里改 `tcp_frame`（如 UR 用 `flange`/`tool0`）。
