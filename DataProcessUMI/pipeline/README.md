# 一键式数据管线（pipeline / run_pipeline）

把一批原始 episode **逐条**串起三步处理，产出**只含可用数据的 `data/` 目录**与
**每条 + 全局报告的 `report/` 目录**：

```
assessment（评估，出报告） → preprocess（轨迹判断 + 平滑/裁剪/拒绝） → transform（world-base 变换 + 翻转腕部视频）
```

## 范围（到哪一步为止）

本管线**到 transform（变换到 world-base 新坐标系 + 翻转腕部视频）为止**。它**不包含
IK 解算，也不包含可执行性求解**：

- IK 解算（把 TCP/EEF 位姿解到关节角）与可执行性求解属于**独立工具**，
  不在本仓库范围内，未接入本管线。

即数据流停在 world-base 位姿这一层；若需要继续解算到关节空间或做可执行性检查，
请使用单独的解算工具。

## 逐条流式处理（架构）

三步**全部在进程内、按 episode 逐条执行**：每条 episode 走完
`assessment → preprocess → transform` 整条流程后，才开始处理下一条——
**不是某一步把全部数据跑完再进入下一步**。

- 第一条 episode 无需等待其余数据评估完，即可直达 transform；步骤之间没有“批处理屏障”。
- 第 1 步校验改为**在主循环内逐条调用** `validate_raw_data.validate_episode(...)`
  （而非先对整批数据跑一遍子进程再回读报告），因此首条输入更快见到产出。
- 单条 episode 出错只影响该条（记为 `error` 并写明原因），不会中断整批。

## 输出布局

```
<output-root>/
├── data/                                  # 仅保留“通过”的可用数据
│   └── <class_name>_w_world_base/         # 类名默认加 _w_world_base 后缀
│       └── episode_XXXX/ ...              # 已平滑/裁剪 + world-base 变换后的 episode
├── report/                                # 报告
│   ├── <class_name>/
│   │   └── episode_XXXX.json              # 每条 episode 的合并报告
│   └── pipeline_report.json              # 全局报告
└── .work/                                 # 中间产物（默认处理完删除，--keep-intermediate 保留）
```

- **`data/`**：每条**通过**的数据，按 `data/<类名>_w_world_base/episode_XXXX/` 存放
  （后缀由 `--suffix` 控制，默认 `_w_world_base`）。被**拒绝**的 episode 不在此出现。
- **`report/<类名>/episode_XXXX.json`**：每条 episode 一份**合并报告**，含三步信息：
  - `assessment`：第 1 步校验结论（`correct` / 各项 `result` / `checks_run`）+ 门禁判定
    `gate`（`blocked` / `blocking_problems` / `tolerated_problems`）；
  - `classification`：第 2 步轨迹分类（`label` / `label_zh` / `category`）；
  - `smoothing`：第 2 步平滑/裁剪操作（`operations` / `interpolated` / `crop` / 原始与保留帧数）；
  - `transform`：第 3 步变换记录（变换的 CSV、翻转的视频、裁剪、输出路径）；
  - `status` + `failed_stage` + `reason`：是否通过、在哪一步被拦截、未通过的原因。
- **`report/pipeline_report.json`**：全局报告——本次处理多少、产出多少（`totals`）、
  按阶段统计被丢弃数（`dropped_by_stage`）、按标签/类别计数，以及每条 episode 的
  `status`（passed / rejected / error）、`failed_stage`、所做处理、未通过原因。

## 三步逻辑与“通过”判定

| 步骤 | 工具 | 作用 | 是否拦截 |
| --- | --- | --- | --- |
| 1. assessment | `assessment/validate_raw_data.py` | 校验原始数据并出结论 | **按规则拦截**（见下） |
| 2. preprocess | `preprocess/preprocess_trajectory.py` | 分类 + 插值平滑 / 裁剪首尾 / 拒绝不可恢复 | **拦截**：被拒绝则不进入第 3 步 |
| 3. transform | `transform/transform_episode_w_world_base.py` | world-base 位姿变换 + 翻转腕部视频 | 出错记为 `error` |

- **passed**：通过第 1 步门禁，第 2 步产出干净数据，第 3 步变换成功 → 数据落在 `data/…_w_world_base/`。
- **rejected**：第 1 步门禁拦截，或第 2 步判为不可用（中部不可恢复 / 裁剪后过短）→ 无数据输出，报告给出原因与所在阶段 `failed_stage`。
- **error**：某一步抛异常 → 无数据输出，报告记录异常信息（单条失败不会中断整批）。

### 第 1 步 assessment 门禁规则

assessment 不是单纯信息性的——它会**按问题类型决定是否放行**：

| 校验问题 | 处理 |
| --- | --- |
| **gripper** 任意问题 | **放行**（继续后续流程） |
| **video 掉帧类**问题（丢帧/重复帧/帧数不匹配：`duplicate_frames_exceed_thresholds` / `missing_timestamps` / `video_frame_count_mismatch`） | **放行** |
| 其他所有问题：video 失焦 `defocused_video`、相机贴错标签 `mislabeled_stream`、左右镜头互换 `wrist_view_lr_swap`、缺文件、时间戳非单调；以及 **action/pose** 任意问题 | **拦截**：在报告中写明原因并**去掉数据**，不进入第 2、3 步 |

> 即“gripper 出问题可继续、video 掉帧可继续，其他情况一律去掉数据”。门禁判定的明细
> （`blocked` / `blocking_problems` / `tolerated_problems`）写入每条报告的 `assessment.gate`。
> 放行的可容忍问题仍如实记录在 `tolerated_problems` 里。
>
> 可容忍的掉帧类问题集合在 `run_pipeline.py` 的 `TOLERABLE_VIDEO_PROBLEMS` 中，可按需增删。
> 校验过程中若某条 episode 抛异常，则该条不做门禁（视为无校验结论、放行进入第 2 步），并在控制台提示。
> `--skip-assessment` 会整体跳过第 1 步，此时不做门禁（全部放行进入第 2 步）。

## 使用方法

```bash
# 整个类别目录（episode 直接位于其下）
python3 run_pipeline.py /path/to/class_name -o pipeline_out

# 任意上层目录：递归发现所有 episode_XXX，镜像类名层级
python3 run_pipeline.py /path/to/dataset_root -o pipeline_out

# 单个 episode
python3 run_pipeline.py /path/to/class_name/episode_0001 -o pipeline_out

# 跳过第 1 步校验、保留中间产物以便排查
python3 run_pipeline.py /path/to/class_name -o pipeline_out --skip-assessment --keep-intermediate

# 第 1 步只跑部分校验（透传给 validate_raw_data.py）
python3 run_pipeline.py /path/to/class_name -o pipeline_out --assessment-args "--skip-focus --skip-motion"
```

输入可以是单个 `episode_XXX` 目录、一个类别目录，或更上层的根目录（递归发现所有
`episode_XXX` 并镜像其类名层级）。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-o, --output-root` | 输出根目录（默认 `pipeline_out`），下含 `data/` 与 `report/`。 |
| `--suffix` | 输出类名后缀（默认 `_w_world_base`）。 |
| `--overwrite` | 若输出根目录已存在则先清空。 |
| `--skip-assessment` | 跳过第 1 步校验（报告中无 `assessment` 段，全部放行）。 |
| `--keep-intermediate` | 保留 `.work/`（第 1 步逐条校验报告 + 第 2 步清洗后的中间 episode）。 |
| `--fps` | 覆盖帧率（默认取 `metadata.json` 的 `fps_config`，否则 30）；同时作用于第 1 步视频校验。 |
| `--preprocess-config` / `--smooth-config` / `--transform-config` | 覆盖各步配置文件。 |
| `--assessment-args "..."` | 透传给 `validate_raw_data.py` 的额外参数（如 `"--skip-focus --skip-motion"`）。 |
| `--no-video` | 仅测试用：第 2 步跳过视频（会破坏第 3 步对裁剪 episode 的腕部翻转）。 |

## 控制台与全局摘要

- 开头打印输入路径、输出根目录、发现的 episode 数，以及
  `[pipeline] assessment -> preprocess -> transform, per episode: N episode(s)`。
- 逐条打印 `[OK|REJECT|ERROR] <类名>/<episode>: <label>`（未通过附原因）。
- 收尾打印 `processed / passed / rejected / error` 计数，并指向 `data/` 与全局报告。

## 依赖

- Python 3，`numpy` / `scipy`（第 2 步插值），`opencv-python`（第 1 步部分校验：失焦/标签/运动一致性），
  `ffmpeg` / `ffprobe`（视频裁剪/翻转）。
- 同仓三步工具，脚本按相对路径自动导入、无需安装：
  - `assessment/validate_raw_data.py`
  - `preprocess/preprocess_trajectory.py`（及其复用的 `smooth_assessment.py`）
  - `transform/transform_episode_w_world_base.py`（及其 `ee_transform.py`）
- 各步细节见各自目录的 README：`assessment/README.md`、`preprocess/PREPROCESS_README.md`、
  `transform/README.md`。
