# 使用手册（USAGE）

本目录提供两个命令行程序，输入一段 TCP（末端工具）轨迹，输出：

- `check_trajectory.py` —— 这段轨迹**能不能执行**（逐点判定 + 汇总 + 退出码）
- `tcp_to_joints.py` —— 把这段轨迹**解算成关节角序列**

> 内部原理与参数全集见 `README.md`；本文件只讲“怎么用”。

---

## 1. 安装

```bash
pip install pin          # 一次性，自带 pinocchio + coal，无需 ROS
```

机型模型在同级 `../resources`。若是新机器首次使用，确认网格软链接存在：

```bash
ls ../resources/.ament/install/share        # 应能看到 ur_description 等软链接
```

支持的机型（`--robot` 取值）：
`franka_fr3v2`、`ur5e`、`ur7e`、`flexiv_rizon4`、`aloha_piper`、`arx5_x5`。

---

## 2. 30 秒上手

先用内置生成器造一段“一定可达”的样例轨迹，再依次跑两个程序：

```bash
# 生成 20 点样例（对 franka 做关节摆动后正向运动学得到 TCP 位姿）
python examples/make_example.py --robot franka_fr3v2 --n 20 --out /tmp/demo.csv

# 程序一：判定可执行性
python check_trajectory.py --robot franka_fr3v2 --input /tmp/demo.csv --time-col t --out report.csv

# 程序二：解算关节序列
python tcp_to_joints.py --robot franka_fr3v2 --input /tmp/demo.csv --time-col t --out joints.csv
```

---

## 3. 准备输入 CSV

每行一个 TCP 目标位姿（TCP frame 相对机器人基座/世界）。`#` 开头为注释行。
**有表头**就按列名取列；**没表头**就按下面的固定列序。

- 默认 `--rot quat`（四元数）：`x y z qx qy qz qw`
- `--rot rpy`（欧拉角，弧度，固定轴 XYZ）：`x y z roll pitch yaw`
- 想做**关节速度校验**，再加一列时间 `t`（秒），并传 `--time-col t`。

带表头的 quat 示例：

```csv
x,y,z,qx,qy,qz,qw,t
0.722149,-0.189332,0.367597,0.84986,0.474986,0.070151,0.217266,0.0
0.762979,-0.158483,0.49193,0.803609,0.489961,0.205772,0.267972,0.1
```

单位：位置 **米**，姿态四元数（会自动归一化）或欧拉角 **弧度**。

---

## 4. 程序一：判定可执行性

```bash
python check_trajectory.py --robot ur5e --input traj.csv --rot quat \
       --time-col t --out report.csv
```

**终端输出（真实示例）**

```
机械臂: franka_fr3v2  点数: 20
可执行: 20/20 (100.0%)
质量: 最差 0.508 / 平均 0.581
最大IK误差: 0.155mm / 0.006deg  最小间隙: 34.10mm  最小奇异值: 0.0919
报告: report.csv   汇总: report.summary.json
```

**产出两个文件**

`report.csv`（逐点，常看这几列）：

| 列 | 看什么 |
|---|---|
| `executable` | 该点能否执行（总判定） |
| `reason` | 不可执行的原因（见下表） |
| `pos_err_mm` / `rot_err_deg` | IK 到达目标的误差 |
| `sigma_min` | 越小越接近奇异 |
| `clearance_mm` | 距自碰撞的余量（<0 即穿透） |
| `vel_ratio` | 关节速度/限位（>1 超速；无时间列为 nan） |
| `quality` | 0~1 综合质量分 |

`report.summary.json`（整段汇总）：

```json
{
  "n_points": 20,
  "n_executable": 20,
  "executable_ratio": 1.0,
  "first_failure_index": null,
  "failure_reasons": {},
  "worst_quality": 0.5078,
  "min_clearance_mm": 34.0952,
  "min_sigma": 0.0919
}
```

**`reason` 取值**：`ik_unreachable`（够不到）、`joint_limit`（越限）、
`self_collision`（自碰）、`near_singular`（近奇异）、`collision_margin`（间隙太小）、
`velocity_limit`（超速）、`ok`（可执行）。

**退出码**：全部可执行→`0`，否则→`1`（方便 `if` 判断 / CI 流水线）。

---

## 5. 程序二：TCP → 关节序列

```bash
python tcp_to_joints.py --robot franka_fr3v2 --input traj.csv --time-col t \
       --deg --out joints.csv
```

**终端输出（真实示例）**

```
机械臂: franka_fr3v2  关节: ['fr3v2_joint1', ..., 'fr3v2_joint7']
求解: 20/20 点 IK 收敛
关节序列已写出: joints.csv  (单位: deg)
```

**`joints.csv`（真实示例）**

```csv
index,t,fr3v2_joint1,fr3v2_joint2,...,fr3v2_joint7,ik_ok,pos_err_mm,rot_err_deg
0,0.0,-5.072399,22.788401,...,-18.458871,1,0.00115,0.00042
1,0.1,-6.685181,20.162155,...,-24.368704,1,0.06784,0.00615
```

- 关节角默认 **弧度**，加 `--deg` 输出 **角度**。
- 沿轨迹热启动，关节序列是**连续**的，可直接喂给控制器。
- 默认输出所有点；够不到的点给的是最优近似解（看 `pos_err_mm`）。
  只要可达点用 `--only-reachable`。

---

## 6. 常用选项速查

| 选项 | 适用 | 说明 |
|---|---|---|
| `--robot` | 两者 | 机型，必填 |
| `--input` | 两者 | 输入 CSV，必填 |
| `--rot quat\|rpy` | 两者 | 姿态格式（默认 quat） |
| `--time-col t` | 两者 | 指定时间列（程序一据此做速度校验） |
| `--out` | 两者 | 输出文件名 |
| `--jobs N` | 两者 | 多进程并行（大批量轨迹提速） |
| `--restarts K` | 两者 | 段首 IK 随机重启次数（默认 4） |
| `--deg` | 程序二 | 关节角以角度输出 |
| `--only-reachable` | 程序二 | 只输出 IK 收敛的点 |
| `--calibrate N` | 程序一 | 采样 N 次自动屏蔽恒碰撞对（无 SRDF 机型建议 200） |
| `--sigma-min` | 程序一 | 近奇异阈值（默认 0.02，调小更宽松） |
| `--clearance-mm` | 程序一 | 碰撞余量阈值（默认 2.0） |
| `--vel-ratio` | 程序一 | 速度上限比（默认 1.0） |
| `--pos-tol-mm` / `--rot-tol-deg` | 两者 | IK 收敛阈值（默认 1.0mm / 0.5°） |

---

## 7. 大批量与并行

十万级轨迹点：用 `--jobs` 吃满多核。无 SRDF 的机型（UR/Flexiv/ARX5）再加
`--calibrate` 先标定恒碰撞对，结果更准：

```bash
python check_trajectory.py --robot flexiv_rizon4 --input big_traj.csv \
       --time-col t --jobs 8 --calibrate 200 --out report.csv
```

轨迹被切成连续块并行处理，块内热启动、块界冷启动；吞吐量随核数近线性提升。

---

## 8. 接入 DataProcessUMI 主流水线

主流水线已经可以在 transform 后自动调用 episode 级 IK / 可执行性求解：

```bash
python3 ../pipeline/run_pipeline.py /path/to/data -o pipeline_out \
    --run-executability --ik-robots ur5e flexiv_rizon4 --ik-arm both --ik-jobs 8
```

该入口会对 `pipeline_out/data/<class>_w_world_base/episode_XXXX` 调用
`executability/solve_executability.py --no-transform`，避免二次坐标变换；结果写入
`pipeline_out/report/<class>/episode_XXXX/executability/`，并嵌入单条 episode 合并报告的
`executability` 字段。

## 9. 手工接入流水线（用退出码判定）

```bash
if python check_trajectory.py --robot ur5e --input traj.csv --out report.csv; then
    echo "整段可执行，下发关节序列"
    python tcp_to_joints.py --robot ur5e --input traj.csv --out joints.csv
else
    echo "存在不可执行点，详见 report.csv 的 reason 列"
fi
```

---

## 9. 常见问题

- **大量 `near_singular`？** 多数机型的“限位中点”恰好是手臂伸展的近奇异位形，
  样例绕其摆动会触发——这是正常的奇异检测。只关心“够不够得到”时调小 `--sigma-min`。
- **够不到（`ik_unreachable`）？** 目标在工作空间外，或姿态约束太苛刻；
  可加大 `--restarts`，或确认位姿单位/参考系正确（位置米、姿态相对基座）。
- **想换 TCP 参考点？** 在 `robots.py` 的注册表里改 `tcp_frame`
  （如 UR 可用 `tool0` 或 `flange`）。
- **mesh could not be found？** 确认 `../resources/.ament/install/share` 软链接在；
  详见 `../resources/README.md`。
