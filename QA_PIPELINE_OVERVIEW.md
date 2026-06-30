# DataPipeline / QA_Pipeline — 机器人与 UMI Episode 数据的多阶段质量检查、事件监听与报告平台

Python CLI、多进程 QA、SQLite、内置 HTTP Dashboard、RabbitMQ/DCS 事件 SDK 与 UMI/机器人运动学工具组成的 report-first 数据质检仓库。主要运行于 Linux/Ubuntu 服务器和 NAS 挂载环境；本地 macOS 也可运行不依赖服务器硬件的检查与测试。生产服务器地址、反向代理和进程托管方式在仓库中未固定，需部署方补充。

| 属性 | 值 |
|---|---|
| 仓库地址 | `https://github.com/A1exan10er/DataPipeline.git` |
| 创建者 | 待补充；Git remote owner 为 `A1exan10er`，不能仅据此确认创建者 |
| 主要开发者 | `Tianyu Yang <yangtianqwer@outlook.com>`、`yejiawei <jackyip719@gmail.com>`（依据当前 Git 历史提交统计） |

## 一、架构详情与技术栈

### 整体架构

以下为当前仓库扫描到的全部 180 个 `.py`/`.sh`/脚本文件。`DataProcessUMI/resources/` 主要是随仓库提供的第三方机器人 SDK、ROS 描述与示例，不属于 QA 业务入口，但仍逐文件列出。

```text
DataPipeline/
  QA_Pipeline/
    scripts/
      align_frames.py                         ← 检查各模态帧数对齐，并可显式执行裁剪
      calibrate_phase5.py                     ← 从已知正常 episode 标定 Phase 5 阈值
      check_tactile_focus.py                  ← 以 Laplacian 方差抽样检查 RGB/触觉视频清晰度
      event_listener_control.sh               ← 用 tmux 启停、重启和查看事件监听服务
      export_excel_report.py                  ← 将现有 QA SQLite 数据导出为 Excel
      generate_dashboard.py                   ← 从 QA SQLite 生成 dashboard.html/dashboard_data.json
      generate_work_session_report.py         ← 生成半日/累计中文质检报告和设备故障统计数据
      live_dashboard.py                       ← 周期刷新静态 Dashboard，并可启动简单 HTTP 服务
      plan_standstill_trim.py                 ← 只生成首尾静止段裁剪计划，不修改源数据
      qa_control_dashboard.py                 ← 中央控制台、REST API、事件队列、报告和 Issues UI
      qa_status.py                            ← 在终端显示最近一次运行的实时状态
      resource_monitor.sh                     ← 采集服务器 CPU、内存和负载监控信息
      run_deferred_umi_phase6.py              ← 从既有 QA DB 批量执行延迟的 UMI Phase 6
      run_pipeline.py                         ← QA Pipeline 主 CLI，负责发现、批处理、阶段调度和报告
      run_umi_ik_batch_with_logging.sh        ← 批量运行 UMI IK 并记录日志
      issue_translations.json                 ← Dashboard check_name 中文说明与 tooltip 数据
      pipeline/
        phase1_metadata.py                    ← 目录、metadata、模态、标签和 task/robot 一致性检查
        phase2_duration.py                    ← 时长、帧/行数一致性与 task 级时长离群检查
        phase3_timestamp.py                   ← 时间戳、FPS、丢帧与多图像模态同步检查
        phase4_video.py                       ← 视频可读性、属性、黑白帧和冻结帧检查
        phase5_robot_state.py                 ← 机器人状态/action 范围、跳变、速度和静止合理性检查
        phase6_umi_processing.py              ← UMI 验证、轨迹预处理、world-frame 导出和可选 IK
        phase7_standstill.py                  ← 采集人员开头/中段/结尾静止内容检查
        qa_config.py                          ← 合并默认配置、JSON 配置和环境覆盖
        qa_core.py                            ← Episode/Finding 模型、发现、SQLite、状态和报表查询公共层
        qa_dcs_notifier.py                    ← 将可操作 QA 异常可选发布到 DCS 事件总线
        resource_guard.py                     ← 根据负载、内存和 worker 上限暂停或停止运行
        run_monitor.py                        ← 写入 run_status、issue_events 和实时摘要
        task_device_reference.py              ← Phase 1 task 与设备类别参考映射
    tests/
      test_consecutive_failure_streaks.py     ← 连续失败分段、历史持久化、resolve API/UI 回归测试
      test_work_session_device_summary.py     ← collector_id、设备汇总、报告路由和容错回归测试
    configs/
      quality_rules.json                      ← 生产 QA 阈值和 Phase 6/7 配置
      quality_rules_umi_test.json             ← UMI 严格测试配置
      quality_rules_umi_ik_test.json          ← UMI IK 测试配置
      report_rule_explanations_zh.json        ← check_name 中文判定规则说明
      work_session_report.json                ← 半日时段、标签和问题处置建议

  Werkzeuge/
    analyze_motion_abnormalities.py           ← 只读运动异常原型分析器
    check_episode_durations.py                ← 递归读取 metadata.json 并报告 episode 时长
    listen_episode_verified.py                ← 监听 verified 事件、维护 jobs.db 并调用 QA Pipeline

  dcp-sdk/
    demo.py                                   ← 监听采集结果验证完成事件的示例
    dcs_sdk/
      __init__.py                             ← DCS SDK 公共导出
      auth.py                                 ← JWT 解码辅助
      config.py                               ← DCS 共享配置加载
      control_events.py                       ← Collector 控制事件 Pydantic 合约
      events.py                               ← RabbitMQ/DCS 事件发布与消费 API
      hardware_events.py                      ← 硬件维修事件合约
      identity.py                             ← collector_id、设备身份和队列名辅助
      nas.py                                  ← NAS 配置辅助
      temporal.py                             ← Temporal 连接和任务队列辅助
    event_center/
      __init__.py                             ← event_center 兼容包入口
      config.py                               ← event center 统一配置辅助
      bus/
        __init__.py                           ← bus 子包导出
        event_bus.py                          ← EventBus 兼容实现
      client/
        __init__.py                           ← client 子包导出
        event_client.py                       ← DCS 事件 SDK 兼容客户端
      events/
        __init__.py                           ← events 子包导出
        base.py                               ← 事件基础模型
    tests/
      test_event_listener.py                  ← 依赖 RabbitMQ 的 EventListener 集成测试

  DataProcessUMI/
    assessment/
      check_focus.py                          ← 检测 wrist-view 视频失焦
      check_label_similarity.py               ← 检测 wrist-view 与 tactile 跨类误标
      validate_raw_data.py                    ← 按配置验证原始 UMI 数据结构与内容
    executability/
      read_episode.py                         ← 读取 episode TCP 末端轨迹
      solve_executability.py                  ← 判断 TCP 轨迹在各机型上的可执行区间
    pipeline/
      run_pipeline.py                         ← UMI assessment→preprocess→transform→可选 executability 总入口
    preprocess/
      preprocess_trajectory.py                ← 按平滑度标签预处理轨迹
      smooth_assessment.py                    ← 评估 actions.eef_pose 轨迹平滑度
    solve/
      batch.py                                ← 多进程、热启动的批量 IK
      check_trajectory.py                     ← 判断 TCP 轨迹是否可执行
      core.py                                 ← IK 求解器与单点指标核心
      fit_trajectory.py                       ← 搜索整段 TCP 轨迹可行平移
      ik_pink.py                              ← IK 交叉验证和可选 Pink QP-IK
      io_poses.py                             ← TCP 位姿 CSV 读取
      robots.py                               ← Pinocchio/Coal 机器人模型注册与加载
      tcp_to_joints.py                        ← 将 TCP 轨迹解算为关节序列
      viz_meshcat.py                          ← MeshCat 回放关节轨迹和目标 TCP 路径
      workspace_bounds.py                     ← 估计各机器人 TCP 工作空间包围盒
      examples/
        make_example.py                       ← 用 FK 生成可达 TCP 示例轨迹
    transform/
      ee_transform.py                         ← 末端位姿/坐标变换辅助
      transform_episode_w_world_base.py       ← 导出 world-base 坐标系 episode
      visualize_episode_w_world_base.py       ← 可视化 world-base 转换结果
    resources/
      _loadtest.py                            ← Pinocchio/Coal 加载机器人 URDF 冒烟测试
      arx5-sdk/
        python/communication/zmq_client.py     ← ARX5 ZMQ 客户端示例
        python/communication/zmq_server.py     ← ARX5 ZMQ 服务端示例
        python/examples/aloha.py              ← ALOHA/ARX 控制示例
        python/examples/arx5_zmq.py           ← ARX5 ZMQ 控制示例
        python/examples/calibrate.py          ← ARX5 标定示例
        python/examples/cartesian_waypoint_scheduling.py ← 笛卡尔航点调度示例
        python/examples/joint_waypoint_scheduling.py ← 关节航点调度示例
        python/examples/keyboard_teleop.py    ← 键盘遥操作示例
        python/examples/spacemouse_teleop.py  ← SpaceMouse 遥操作示例
        python/examples/teach_replay.py       ← 示教回放示例
        python/examples/test_bimanual.py      ← 双臂控制测试
        python/examples/test_gripper_force_compensation.py ← 夹爪力补偿测试
        python/examples/test_joint_control.py ← 关节控制测试
        python/examples/test_solver.py        ← SDK 求解器测试
        python/examples/test_torque_control.py ← 力矩控制测试
        python/examples/test_upside_down.py   ← 倒装姿态测试
        python/examples/test_x7.py            ← X7 机型测试
        python/peripherals/keystroke_counter.py ← 键盘事件计数辅助
        python/peripherals/spacemouse_shared_memory.py ← SpaceMouse 共享内存接口
        python/shared_memory/shared_memory_queue.py ← 共享内存队列
        python/shared_memory/shared_memory_ring_buffer.py ← 共享内存环形缓冲区
        python/shared_memory/shared_memory_util.py ← 共享内存公共函数
        python/shared_memory/shared_ndarray.py ← 共享 ndarray 封装
        wheels/__init__.py                    ← ARX5 wheel 包标记
        wheels/build_wheel_single_ver.sh      ← 构建单 Python 版本 wheel
        wheels/build_wheels.sh                ← 批量构建 wheel
        wheels/setup.py                       ← ARX5 wheel setuptools 配置
        wheels/upload_to_pypi.sh              ← 上传 wheel 到 PyPI
      flexiv_description/
        .docker/create_urdf.entrypoint.sh     ← Flexiv Docker URDF 生成入口
        .docker/visualize_rizon.entrypoint.sh ← Flexiv Docker 可视化入口
        launch/view_aico1.launch.py           ← ROS 2 查看 AICO1
        launch/view_aico2.launch.py           ← ROS 2 查看 AICO2
        launch/view_rizon.launch.py           ← ROS 2 查看单 Rizon
        launch/view_rizon_dual.launch.py      ← ROS 2 查看双 Rizon
        scripts/create_urdf.py                ← 生成 Flexiv URDF
        scripts/create_urdf.sh                ← Flexiv URDF 生成包装脚本
        scripts/visualize_rizon.sh            ← 启动 Rizon 可视化
      franka_description/
        .ci/run_urdf_tests.sh                 ← Franka URDF CI 测试入口
        .docker/create_urdf.entrypoint.sh     ← Franka Docker URDF 生成入口
        .docker/visualize_franka.entrypoint.sh ← Franka Docker 可视化入口
        .docker/visualize_franka_duo.entrypoint.sh ← 双 Franka Docker 可视化入口
        launch/visualize_franka.launch.py     ← ROS 2 Franka 可视化 launch
        scripts/create_urdf.py                ← 生成 Franka URDF
        scripts/create_urdf.sh                ← Franka URDF 生成包装脚本
        scripts/visualize_franka.sh           ← 启动 Franka 可视化
        test/urdf_tests.py                    ← Franka URDF 测试
      piper_ros/
        can_activate.sh                       ← 激活单 Piper CAN
        can_config.sh                         ← 配置 Piper CAN
        can_muti_activate.sh                  ← 激活多 Piper CAN
        find_all_can_port.sh                  ← 枚举 CAN 端口
        src/piper/launch/start_single_piper.launch.py ← 启动单 Piper ROS 节点
        src/piper/launch/start_single_piper_rviz.launch.py ← 启动单 Piper 与 RViz
        src/piper/launch/start_two_piper.launch.py ← 启动双 Piper ROS 节点
        src/piper/piper/__init__.py            ← Piper ROS Python 包标记
        src/piper/piper/piper_ctrl_single_node.py ← 单 Piper 控制节点
        src/piper/piper/piper_ctrl_single_node_new.py ← 新版单 Piper 控制节点
        src/piper/piper/piper_read_slave_joint.py ← 读取 Piper 从臂关节
        src/piper/setup.py                     ← Piper ROS Python 包配置
        src/piper/test/test_copyright.py       ← ROS 包版权检查
        src/piper/test/test_flake8.py          ← ROS 包 Flake8 检查
        src/piper/test/test_pep257.py          ← ROS 包 docstring 检查
        src/piper_description/launch/piper_no_gripper/display_no_gripper_urdf.launch.py ← 显示无夹爪 URDF
        src/piper_description/launch/piper_no_gripper/display_no_gripper_urdf_follow.launch.py ← 显示并跟随无夹爪 URDF
        src/piper_description/launch/piper_no_gripper/display_no_gripper_xacro.launch.py ← 显示无夹爪 Xacro
        src/piper_description/launch/piper_with_gripper/display_urdf.launch.py ← 显示带夹爪 URDF
        src/piper_description/launch/piper_with_gripper/display_urdf_follow.launch.py ← 显示并跟随带夹爪 URDF
        src/piper_description/launch/piper_with_gripper/display_xacro.launch.py ← 显示带夹爪 Xacro
        src/piper_description/launch/piper_with_teach/display_with_teach_urdf.launch.py ← 显示示教臂 URDF
        src/piper_moveit/piper_no_gripper_moveit/launch/demo.launch.py ← 无夹爪 MoveIt demo
        src/piper_moveit/piper_no_gripper_moveit/launch/move_group.launch.py ← 无夹爪 move_group
        src/piper_moveit/piper_no_gripper_moveit/launch/moveit_rviz.launch.py ← 无夹爪 MoveIt RViz
        src/piper_moveit/piper_no_gripper_moveit/launch/piper_moveit.launch.py ← 无夹爪 MoveIt 总入口
        src/piper_moveit/piper_no_gripper_moveit/launch/rsp.launch.py ← 无夹爪 robot_state_publisher
        src/piper_moveit/piper_no_gripper_moveit/launch/setup_assistant.launch.py ← 无夹爪 Setup Assistant
        src/piper_moveit/piper_no_gripper_moveit/launch/spawn_controllers.launch.py ← 无夹爪控制器启动
        src/piper_moveit/piper_no_gripper_moveit/launch/static_virtual_joint_tfs.launch.py ← 无夹爪虚拟关节 TF
        src/piper_moveit/piper_no_gripper_moveit/launch/warehouse_db.launch.py ← 无夹爪 MoveIt warehouse
        src/piper_moveit/piper_with_gripper_moveit/launch/demo.launch.py ← 带夹爪 MoveIt demo
        src/piper_moveit/piper_with_gripper_moveit/launch/move_group.launch.py ← 带夹爪 move_group
        src/piper_moveit/piper_with_gripper_moveit/launch/moveit_rviz.launch.py ← 带夹爪 MoveIt RViz
        src/piper_moveit/piper_with_gripper_moveit/launch/piper_moveit.launch.py ← 带夹爪 MoveIt 总入口
        src/piper_moveit/piper_with_gripper_moveit/launch/rsp.launch.py ← 带夹爪 robot_state_publisher
        src/piper_moveit/piper_with_gripper_moveit/launch/setup_assistant.launch.py ← 带夹爪 Setup Assistant
        src/piper_moveit/piper_with_gripper_moveit/launch/spawn_controllers.launch.py ← 带夹爪控制器启动
        src/piper_moveit/piper_with_gripper_moveit/launch/static_virtual_joint_tfs.launch.py ← 带夹爪虚拟关节 TF
        src/piper_moveit/piper_with_gripper_moveit/launch/warehouse_db.launch.py ← 带夹爪 MoveIt warehouse
        src/piper_sim/piper_gazebo/launch/piper_no_gripper/piper_no_gripper_gazebo.launch.py ← 无夹爪 Gazebo 仿真
        src/piper_sim/piper_gazebo/launch/piper_with_gripper/piper_gazebo.launch.py ← 带夹爪 Gazebo 仿真
        src/piper_sim/piper_gazebo/scripts/joint8_ctrl.py ← Gazebo 第八关节控制
        src/piper_sim/piper_mujoco/scripts/piper_mujoco_ctrl.py ← Piper MuJoCo 控制
        src/piper_sim/piper_mujoco/scripts/piper_no_gripper_mujoco_ctrl.py ← 无夹爪 Piper MuJoCo 控制
      universal_robots/
        doc/conf.py                            ← Universal Robots Sphinx 文档配置
        launch/view_ur.launch.py               ← ROS 2 查看 UR 模型
        test/test_ur_urdf_xacro.py             ← URDF/Xacro 测试
        test/test_view_ur_launch.py            ← UR launch 测试

  UMI_Data_Validation/
    deploy_ubuntu.sh                           ← 在 Ubuntu 部署 UMI 验证环境
    ik_benchmark.py                            ← UMI IK 性能/正确性基准原型

  annotate_standstill.py                       ← 在 CSV 中检测并标注超过缓冲时长的静止段
  clean_invalid_episodes.py                    ← 扫描不合规 episode 名称并可移动到隔离区
  correct_teleop_folders.py                    ← 将误放的 TCP action 目录更名为 actions.eef_pose
  run_cleanup.sh                               ← 面向 cron 的无效 episode 清理包装脚本
  seed_jobs.sh                                 ← 本地测试：向 jobs.db 批量写入模拟 done jobs
  simulate_live_jobs.sh                        ← 本地测试：按时间间隔模拟事件 job 到达
  trickle_jobs.sh                              ← 本地测试：从 qa.db 逐条灌入未使用 job
  trickle_jobs_compat.sh                       ← 本地测试：兼容 macOS Bash 的逐条灌入脚本
```

### 技术栈

| 组件 | 技术 | 说明 |
|---|---|---|
| Web 框架/CLI 入口 | Python `argparse`、`http.server.ThreadingHTTPServer` | 无外部 Web 框架；`run_pipeline.py` 是 QA 主入口，`qa_control_dashboard.py` 在默认 `0.0.0.0:4131` 提供控制台 |
| 数据库 | SQLite (`sqlite3`) | QA 状态、finding、事件 job、Dashboard run registry 和问题历史均为独立 SQLite 文件 |
| ORM/数据访问 | Python `sqlite3.Row` + 手写 SQL | 无 ORM；批量查询按 400/500 路径分块，避免 SQLite 参数过多 |
| 并行/异步框架 | `multiprocessing.Pool`、`asyncio`、线程 | Phase 1–5/7 可多进程；事件监听用 asyncio；Dashboard 用线程 HTTP 和后台报告线程 |
| 视频/数值 | OpenCV、SciPy、NumPy、FFmpeg/ffprobe | Phase 4 视频检查、Phase 6 UMI 处理和轨迹计算 |
| 机器人/IK | Pinocchio (`pin`)、Coal、Xacro、MeshCat（工具链） | UMI executability、机器人模型加载、IK 与可视化 |
| 消息与工作流 | RabbitMQ (`aio-pika`)、Pydantic、JWT、Temporal helper | DCS verified 事件、异常通知和共享事件合约 |
| 报告 | CSV、JSONL、Markdown、HTML、可选 openpyxl | 主运行不依赖 Excel；Excel 由独立脚本导出 |
| 运行控制 | tmux、Shell、stop file、resource guard | 中央 Dashboard 启停运行；资源不足时暂停/停止；支持安全停止和 resume |

## 二、实现的功能点

### 数据发现与多阶段校验

| 功能 | 具体实现 | 说明 |
|---|---|---|
| Episode 发现 | `discover_episodes_with_report()` 递归识别 `episode_*`，报告隐藏目录 | 支持旧版 `<task>/<date>/<operator>` 和新版 `<task>/<robot_type>/<collector_id>/<date>/<operator>` 路径 |
| 输入过滤 | `--episode-list`、`--date/--date-from/--date-to`、`--task`、`quality.labels` | 默认只处理 metadata 中含 `完全正常` 的 episode；完整审计需显式关闭过滤 |
| Phase 1 | 目录名、metadata JSON、必需字段/模态/文件、checksum、标签、task/robot 类别 | `metadata.json` 无效时停止该 episode 的 Phase 1 后续检查 |
| Phase 2 | metadata 时长、帧/行数、视频/action 差值、task 时长离群 | task 级统计要求 group-aware batch 保持分组完整 |
| Phase 3 | 时间戳递增、频率偏差、FPS 损失、丢帧率/连续丢帧、起止同步 | 可优先使用 `metadata.frame_integrity`，必要时读取 timestamps.csv |
| Phase 4 | MP4 可打开性、属性、黑帧、白帧、冻结帧 | NAS 随机 seek 成本较高，通常建议降低 worker |
| Phase 5 | joint/gripper/action 范围、跳变、速度、轨迹与静止合理性 | 阈值由 `quality_rules.json` 和机器人类别配置驱动 |
| Phase 6 | UMI raw assessment、平滑预处理、world-frame 转换、可选 IK | 可由主流程延迟为 `umi_pending` 后独立运行 |
| Phase 7 | 开头、中段、结尾静止段及严重度 | 依据 motion delta、buffer、warning/review/fail 时长阈值 |
| report-first | 主 QA 只分类和报告 | 主流程不会删除源数据、移动 episode 或裁剪视频；修复工具必须单独显式运行 |

### Phase 1–3 并行与批处理

| 功能 | 具体实现 | 说明 |
|---|---|---|
| 多进程 | 三个 phase 在 `workers > 1` 时使用 `multiprocessing.Pool` | worker 返回可 pickle 的 finding dict/metrics，主进程统一落库 |
| 固定 batch | `--batch-mode fixed --batch-size N` | 限制内存占用，每批完成后释放 state 并回收 |
| 分组 batch | `--batch-mode group-aware` | Phase 2 保持 task，Phase 3 保持 task+robot 分组完整 |
| 自动模式 | `--batch-mode auto` | 选择 Phase 2/3 时使用 group-aware，否则 fixed |
| 流式发现 | `--streaming-discovery --batch-size N` | 不构建全量 episode 选择缓存，适合超大 NAS root |
| Resume | 复用同一 `db-path/output-dir` 且不传 `--force-rerun` | `phases_completed` 和 `phase_status` 决定跳过已完成阶段 |
| 资源保护 | `ResourceGuard.effective_workers()` 和运行前/中检查 | 默认 load ratio `0.75`、可用内存 `3 GB`，资源恢复后继续或按配置停止重试 |

### SQLite 数据与查询优化

| 数据库/表 | 字段或索引 | 用途 |
|---|---|---|
| QA DB `episodes` | `episode_path` PK；`task,date,operator,robot,controller,phases_completed,phase_status,metrics,final_status,training_ready,last_updated` | 每个 episode 的累计状态和 JSON metrics |
| QA DB `findings` | `id` PK；`episode_path,phase,check_name,severity,status,message,details` | 每条检查结果；`details` 为 JSON 文本 |
| QA 索引 | `idx_findings_episode_path/phase/status`、`idx_episodes_final_status/task` | Dashboard、报告、状态统计与按 episode 查询 |
| `outputs/event_listener/jobs.db` / `jobs` | event/record/session、verified/mounted path、status、attempts、run/output/db/error 和时间戳 | pending→running→done/failed/retry/skipped 事件工作队列 |
| jobs 索引 | `idx_jobs_status(status,id)`、唯一 `idx_jobs_mounted_path` | 快速 claim 队列并防止同 mounted path 重复 |
| `outputs/dashboard_manager/runs.db` | `runs`、`run_events` | Dashboard 发起的 run、tmux session、命令和事件审计 |
| `outputs/dashboard_manager/issue_history.db` | `consecutive_failure_streaks` | 连续 QA 失败问题的检测、解决和耗时历史 |
| streak 索引 | 未解决 full identity 唯一索引；`resolved_at,detected_at,id` 索引 | 防重复未解决记录和最近问题排序 |
| 查询分块 | 报告每 500 个 episode、Dashboard 每 400 个 episode 查询 finding | 控制 SQL `IN` 参数数量和大查询内存 |
| Dashboard TTL cache | `cached_value()` + `RLock` | `/api/status` 最少缓存 3 秒；操作后 `clear_cache()` |

`consecutive_failure_streaks` 精确字段：

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
task TEXT NOT NULL
robot TEXT NOT NULL
operator TEXT NOT NULL
episode_start INTEGER NOT NULL
episode_end INTEGER NOT NULL
streak_length INTEGER NOT NULL
issue_types TEXT NOT NULL
detected_at TEXT NOT NULL
resolved_at TEXT
resolution_time_seconds INTEGER
```

### Dashboard、事件监听与报告

| 功能 | 具体实现 | 说明 |
|---|---|---|
| 中央控制台 | 单文件 HTML/CSS/inline JS，由 `qa_control_dashboard.py` 动态生成 | 响应设置 `Cache-Control: no-store`；默认 UI refresh 5 秒 |
| Run 管理 | 选择 phases/workers/batch/date/task 后通过 tmux 启停 | 注册到 `runs.db`；无 tmux 环境下对 `FileNotFoundError` 降级 |
| Event Listener | DCS `collector_platform.episode_verified` 事件→jobs.db→QA 子进程 | NAS path 映射支持 `/volume1/database/verified`、`/database/verified` 和本地 mount root |
| Event Jobs UI | 状态/QA/task/robot/episode/issues/updated 排序 | 支持 issue-only、详情 modal、日志、mounted path→NAS internal path 复制 |
| 翻译与时区 | `issue_translations.json`、`localTimestamp()` | check_name tooltip 中文化；ISO 时间在浏览器转换为本地时区 |
| 连续失败检测 | 按 `(task,robot,operator)` 和 episode number 找最大连续 bad segment | 仅 `job.status=done` 且 `episodes.final_status∈{fail,needs_review}`；阈值 5；gap/pass 打断 |
| Issues 历史 | 首次检测写 `issue_history.db`，两次点击确认 resolve | active 最多显示 20 个组合；解决后记录 `resolution_time_seconds`；同 identity 不重开 |
| Resolve UI 回归 | stable identity、客户端即时删除、旧 refresh 抑制 | 避免 DOM 节点替换导致二次确认失效，也避免旧 `/api/status` 响应恢复已解决项 |
| 半日报告 | 上午 09:00–12:00、下午 13:30–18:00，可自定义 | 输出 `report.json`、Markdown 和多个 CSV；事件报告默认每 600 秒自动刷新 |
| 设备故障统计 | metadata `collector_id` 优先，读取失败才按路径 fallback | 只统计 finding `fail/needs_review`；单次报告按 episode path 缓存；主报告 top 10，详情页展示全量与 >70% 单问题风险 |
| 静态 Dashboard | `generate_dashboard.py` + `live_dashboard.py` | 独立于中央控制台，可直接服务 output-dir |

### 回归测试

| 功能 | 具体实现 | 说明 |
|---|---|---|
| 连续失败分段 | 5 条、长 segment、分离 segment、episode gap、短 segment | 验证只产生最大连续段，不产生滑动窗口重叠记录 |
| 历史语义 | resolve 后相同 range 不重插；已解决前缀增长只生成新 suffix | 使用临时 SQLite，不写真实 jobs/qa DB |
| Resolve API/UI | 临时 HTTP server POST、`resolved_at`、返回 count、stable key 和 stale refresh 过滤 | 端到端验证 API 路由和持久化 |
| collector_id | metadata 优先、缺字段 unknown、文件/权限/编码/JSON 错误 fallback | 验证每路径缓存、排序、check 分解和 >70% flag |
| 报告展示 | report.json、Markdown top 10、详情全量、report key 路由 | 验证 URL 编码和目录穿越拒绝 |

## 三、对外接口

### REST/HTML 接口

中央 Dashboard 默认由 `python3 QA_Pipeline/scripts/qa_control_dashboard.py --host 0.0.0.0 --port 4131` 启动。生产域名、TLS 和反向代理：待补充。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/`、`/index.html` | 中央控制台 HTML |
| GET | `/event-listener/work-session-report.html?report=<report_key>` | 指定或最新事件半日报告 |
| GET | `/event-listener/device-failure-report.html?report=<report_key>` | 同一 report.json 的完整设备故障详情 |
| GET | `/api/status` | 服务器资源、listener、streak 和 runs 汇总 |
| GET | `/api/runs` | Dashboard run 列表 |
| GET | `/api/runs/<run_id>` | 单个 run 详情 |
| GET | `/api/event-listener/status` | tmux、jobs count、settings 和输出大小 |
| GET | `/api/event-listener/issue-summary?limit=N` | 最近 listener 问题汇总；limit 最大 500 |
| GET | `/api/event-listener/work-session-report` | 最新事件半日报告 payload |
| GET | `/api/event-listener/jobs?limit=N&issues_only=0|1` | 最近 event jobs |
| GET | `/api/event-listener/jobs/<id>` | job、QA 结果、finding 和日志详情 |
| GET | `/api/server-load` | load、memory 和 top processes |
| GET | `/api/log-tail?target=<run_id|event_listener>` | 指定日志尾部 |
| POST | `/api/start` | 根据 JSON 参数启动 Dashboard run |
| POST | `/api/stop/<run_id>` | 停止对应 tmux run |
| POST | `/api/runs/<run_id>/work-session-report` | 为指定 run 生成累计/时段报告 |
| POST | `/api/event-listener/start` | 启动 listener tmux session |
| POST | `/api/event-listener/stop` | 停止 listener tmux session |
| POST | `/api/event-listener/restart` | 重启 listener tmux session |
| POST | `/api/event-listener/work-session-report` | 为 listener 生成半日报告 |
| POST | `/api/consecutive-failures/resolve` | 按 task/robot/operator/episode_start/episode_end 解决 streak |

Resolve JSON 示例：

```json
{
  "task": "Clean_the_plate_UMI",
  "robot": "arx5",
  "operator": "liangyunbo",
  "episode_start": 9,
  "episode_end": 31
}
```

### QA Pipeline 命令行接口

| 参数 | 含义 |
|---|---|
| `--roots ROOT [ROOT ...]` | 必填，一个或多个扫描根目录 |
| `--episode-list FILE` | 精确 episode 路径清单；跳过常规/流式发现 |
| `--db-path PATH` | SQLite 状态库；默认 `outputs/qa_pipeline.db` |
| `--output-dir DIR` | 报告输出目录；默认 `outputs` |
| `--phases 1,2,...` | 选择阶段；未提供时由代码默认阶段逻辑决定 |
| `--max-episodes N` | 限制 episode 数 |
| `--batch-size N` | 每批加载 state 数；未提供则一次加载全部选择集 |
| `--batch-mode auto|fixed|group-aware` | batch 策略；默认 `auto` |
| `--streaming-discovery` | 以 batch 增量发现/处理，必须同时设置 batch-size |
| `--force-rerun` | 忽略已完成阶段，强制重跑 |
| `--dry-run` | 仅发现和筛选，不运行阶段/写报告 |
| `--continue-after-fail` | 前序 phase fail 后仍继续后续 phase |
| `--date/--date-from/--date-to` | 按路径中的 YYYYMMDD 精确/闭区间过滤 |
| `--task TEXT` | task 名称不区分大小写包含过滤 |
| `--quality-label LABEL` | 默认 `完全正常` |
| `--disable-quality-label-filter` | 不按 metadata quality.labels 过滤 |
| `--disable-episode-selection-cache` | 强制重新扫描选择集 |
| `--workers N` | 请求的进程数；resource guard 可下调，默认 1 |
| `--disable-resource-guard` | 关闭资源保护，不建议共享服务器使用 |
| `--max-load-ratio R` | 1 分钟 load/CPU 上限，默认 0.75 |
| `--min-free-mem-gb GB` | 最低可用内存，默认 3.0 GB |
| `--resource-check-interval SEC` | 资源检查间隔，默认 30 秒 |
| `--resource-max-wait-seconds SEC` | 最长等待恢复；0 表示无限等待 |
| `--resource-error-retries N` | resource stop 后阶段重试次数，默认 3 |
| `--resource-retry-delay-seconds SEC` | 重试延迟，默认 30 秒 |
| `--overload-action pause|stop` | 超载行为，默认 pause |
| `--max-workers-safe N` | resource guard worker 上限；默认 CPU 核数一半 |
| `--run-id ID` | live monitor run ID；默认本地时间戳 |
| `--live-report-interval SEC` | live monitor 刷新间隔，默认 2.0 |
| `--live-dashboard-interval SEC` | 仅影响打印出的独立 dashboard 命令，默认 5 |
| `--live-dashboard-max-episodes/findings N` | 独立 dashboard 明细上限；0 为不限 |
| `--disable-live-monitor` | 不写 run_status/issue_events/live_summary |
| `--defer-umi-phase6` | Phase 6 标为 `umi_pending`，交给 deferred worker |
| `--stop-file PATH|none` | 安全停止文件；默认 `<output-dir>/STOP_REQUESTED` |

### Event Listener 命令行接口

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `serve` | 同时监听事件和处理队列 | listener + worker 全部参数 |
| `listen` | 只收事件写 jobs.db | `--dc-root --queue-name --routing-key --event-date[-from/-to]` |
| `worker` | 只处理 pending/retry jobs | `--qa-python --phases --workers --batch-size --max-attempts` 等 |
| `status` | 打印队列状态 | `--job-db --limit` |
| `enqueue` | 手工插入 verified path | `--verified-path` 必填，可传 event/record/session id |
| `recover-running` | 将中断遗留 running job 重新排队 | `--job-db` |

公共默认值：`job-db=outputs/event_listener/jobs.db`、`mount-prefix=/mnt/nas/database/verified`；worker 默认 phases `1,2,3,7`、workers 1、batch-size 1、max-attempts 3、poll 10 秒、稳定性间隔 5 秒/超时 600 秒、输出保留 14 天。

### 其他主要 CLI 与配置接口

| 模块/文件 | 接口 |
|---|---|
| `qa_control_dashboard.py` | `--host` 默认 `0.0.0.0`、`--port` 默认 `4131`、registry/output/verified/python 路径、refresh 默认 5 秒、自动半日报告默认 600 秒 |
| `generate_work_session_report.py` | `--db-path` 必填；`--session forenoon|afternoon|current|previous`；或成对 `--start/--end`；可替换 config/rule explanations |
| `live_dashboard.py` | `--db-path --output-dir` 必填；interval、明细上限、host/port、`--once --force` |
| `export_excel_report.py` | `--db-path` 和 `--output` 必填 |
| `run_deferred_umi_phase6.py` | 既有 db/output、workers、batch、资源 guard、monitor 和 stop-file 参数 |
| `quality_rules.json` | Phase 1/2/3/5/6/7 规则与阈值；`QA_PIPELINE_CONFIG` 可指向其他 JSON（由 `qa_config.py` 读取） |
| `work_session_report.json` | 上午/下午时段、severity/status 中文标签、check_name action/owner/impact |
| `report_rule_explanations_zh.json` | 报告中的规则标题、判定标准、阈值路径和证据字段 |
| `issue_translations.json` | Dashboard tooltip 的 check_name→中文说明映射 |

## 四、能力边界与职责

### 职责范围

- 发现 NAS 或显式清单中的机器人/UMI episode，并解析路径和 metadata 上下文。
- 运行七个可选择 QA phase，产生结构化 finding、episode verdict 和训练可用性判断。
- 用 SQLite 持久化增量状态，支持 batch、resume、force rerun、资源保护和安全停止。
- 生成 CSV、JSONL、Markdown、HTML、工作时段报告和可选 Excel。
- 监听 DCS verified 事件，维护本地 job 队列并调用 QA 子进程。
- 在 Dashboard 中展示队列、运行、finding、翻译、日志、服务器负载、连续失败和设备故障统计。
- 对 UMI 数据调用仓库内 DataProcessUMI assessment/preprocess/transform/IK 工具。

### 不在职责范围

- 机器人数据采集和 metadata 生产 → 由 collector/DCS 上游负责；本仓库只读取和验证。
- NAS 存储、同步、权限、备份和容量管理 → 由 NAS/基础设施负责；listener 只做路径映射与稳定性等待。
- 主 QA 运行中删除、移动、重命名或裁剪源 episode → 不执行；`clean_invalid_episodes.py`、`correct_teleop_folders.py`、`align_frames.py` 等是独立显式工具。
- 模型训练、数据集发布和 ModelScope 上传 → 当前代码未提供生产上传流程，待其他系统负责。
- RabbitMQ、Temporal、tmux、反向代理和 TLS 的部署运维 → 本仓库提供客户端/控制脚本，不负责服务端基础设施。
- 自动维修硬件 → 设备故障统计只提供证据和风险提示，处理由设备维护人员完成。

### 运行环境

- Python `>=3.10`（DCS SDK 明确要求）；仓库示例使用 `datapipeline-env/`。
- Python 依赖：`opencv-python-headless>=4.13`、`scipy>=1.15`、`openpyxl>=3.1`、`tqdm>=4.66`、`pin>=3.4`、`xacro>=2.1`、`psycopg[binary]>=3.2`、`aio-pika>=9.0`、`pydantic>=2.0`、`python-jose>=3.0`。
- Phase 6 需要主机可执行 `ffmpeg`/`ffprobe`；IK/机器人工具依赖相应 URDF、Pinocchio/Coal。
- 生产数据通常挂载在 `/mnt/nas/database/verified`；NAS 内部路径通常为 `/database/verified` 或 `/volume1/database/verified`。
- Dashboard 默认端口 4131；生产 IP/域名、systemd/tmux 启动策略：待补充。
- `outputs/`、虚拟环境、测试样本和 Python cache 已在 `.gitignore` 中排除。

## 五、对接指南

### 1. 安装并运行 Phase 1–3

```bash
cd /path/to/DataPipeline
source datapipeline-env/bin/activate
python3 -m pip install -r QA_Pipeline/requirements.txt

python3 QA_Pipeline/scripts/run_pipeline.py \
  --roots /mnt/nas/database/verified \
  --date 20260630 \
  --phases 1,2,3 \
  --db-path outputs/qa_20260630/qa_pipeline.db \
  --output-dir outputs/qa_20260630 \
  --workers 3 \
  --batch-size 5000 \
  --batch-mode auto \
  --min-free-mem-gb 4 \
  --max-load-ratio 1.2
```

需要完整审计非“完全正常”数据时，追加：

```bash
--disable-quality-label-filter
```

### 2. 启动中央 Dashboard

```bash
python3 QA_Pipeline/scripts/qa_control_dashboard.py \
  --host 127.0.0.1 \
  --port 4131 \
  --verified-root /mnt/nas/database/verified
```

浏览器访问 `http://127.0.0.1:4131/`。如果通过 SSH 访问远程服务器，可在本机建立端口转发：

```bash
ssh -L 4131:127.0.0.1:4131 <user>@<server>
```

其中 `<user>`、`<server>` 为部署信息，仓库中未定义。

### 3. 手工插入并处理 verified job

```bash
python3 Werkzeuge/listen_episode_verified.py enqueue \
  --verified-path /database/verified/<task>/<robot_type>/<collector_id>/<YYYYMMDD>/<operator>/episode_0001

python3 Werkzeuge/listen_episode_verified.py worker \
  --once \
  --phases 1,2,3,7 \
  --workers 1
```

### 4. 查询 SQLite

查看 episode verdict：

```bash
sqlite3 outputs/qa_20260630/qa_pipeline.db "
SELECT final_status, COUNT(*)
FROM episodes
GROUP BY final_status
ORDER BY COUNT(*) DESC;
"
```

查看某类 issue：

```bash
sqlite3 -header -column outputs/qa_20260630/qa_pipeline.db "
SELECT episode_path, phase, check_name, severity, status, message
FROM findings
WHERE check_name = 'frame_drop_ratio' AND status != 'pass'
ORDER BY severity DESC, episode_path;
"
```

查看未解决连续失败：

```bash
sqlite3 -header -column outputs/dashboard_manager/issue_history.db "
SELECT task, robot, operator, episode_start, episode_end,
       streak_length, issue_types, detected_at
FROM consecutive_failure_streaks
WHERE resolved_at IS NULL
ORDER BY detected_at DESC;
"
```

### 5. 解读 finding

`Finding` 的持久化形态如下。以下数值和 message 取自本地测试 QA DB 的真实 `frame_drop_ratio` 记录，仅将 `episode_path` 脱敏为占位符：

```json
{
  "episode_path": "<episode_path>",
  "phase": 3,
  "check_name": "frame_drop_ratio",
  "severity": "major",
  "status": "fail",
  "message": "Frame drop ratio exceeds configured hard threshold.",
  "details": {
    "modality": "observation.image.left_wrist_left_tactile",
    "drop_ratio": 0.16091954022988506,
    "threshold": 0.15,
    "total_drops": 616,
    "frame_count": 3828
  }
}
```

- `check_name` 是稳定的机器接口；中文解释由 `issue_translations.json` 和 `report_rule_explanations_zh.json` 提供。
- `severity` 表示影响级别：`info/minor/major/critical`。
- `status` 表示判定：`pass/warning/needs_review/fail`。
- episode 最终状态保存在 `episodes.final_status`；不要用 listener 的 `jobs.status=failed` 代表 QA fail。连续失败只接受 `jobs.status=done` 且最终 QA 状态为 `fail/needs_review`。

### 6. 运行回归测试

```bash
python3 -m py_compile \
  QA_Pipeline/scripts/qa_control_dashboard.py \
  QA_Pipeline/scripts/generate_work_session_report.py

python3 QA_Pipeline/tests/test_consecutive_failure_streaks.py
python3 QA_Pipeline/tests/test_work_session_device_summary.py
```

`test_consecutive_failure_streaks.py` 会绑定临时 localhost 端口验证真实 resolve HTTP 路由；受限沙箱中需要允许本地 socket。
