# 本地验收记录 · 2026-09-28

实现与本地回归已完成，预览保持运行。**实时收件、独立准确率、人工效率三项业务验收仍为证据不足**。本报告不授权部署或真实通知，也不声称排除了所有潜在缺陷。

## 预览与证据

- [合成轨迹案例](http://127.0.0.1:1344/pipeline-review) 与 [片段质检](http://127.0.0.1:1344/)：正常、小幅漂移、大幅漂移、软问题、硬性失败、证据不足、失效及标定后持续漂移，共 8 条。
- [真实收件预览](http://127.0.0.1:1345/pipeline-review)：两条真实历史录制完成本地 phases 1、2、3、7、8；均保留待检查状态。历史回放不是实时收件。
- [标注与比较操作指南](/home/tianyu/Projects/DataPipeline/docs/local-labeling-guide.md)。本地测试账号 `local-reviewer` / `local-preview-only`。
- [验证汇总](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/verification.json)、[重启证据](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/restart-recovery.json)、[源文件与构建指纹](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/source-provenance.json)。
- [合成数据完整证据](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/synthetic-evidence.zip)、[真实历史完整证据](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/historical-evidence.zip)、[真实历史逐片段追溯](/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/historical-traces.json)。

完整 ZIP 来自一致的本地 SQLite 快照，逐表分块导出收件、事件、显示回执、结果与历史版本、审核与标定记录、提醒记录及证据失效记录。结果负载保留有效配置和输入清单；附带人工标注和比较工作表。导出清单逐表提供数量与 SHA256，已独立校验；没有登录令牌或原始视频。旧 JSON 下载保留兼容，并明确标记截断及完整 ZIP 入口。

## 本次修复

- 增加 session、queue、trace 和 monitor 请求的取消与超时；事件刷新合并并保留后续刷新。事件游标、未完成显示回执持久化，分页轮询可跨过 200 条并与 SSE 重连协作。
- 上游收件身份保留 `(event_id, path)`；重复读取不建新作业。真实重传使旧证据失效，替代链保留旧作业与新结果的对应关系，不再因已完成替代而永久阻断验收。
- NAS 重验移出数据库写事务，提交时核对证据版本。权限动作继续执行即时新鲜度检查。
- 停机终止本地检查进程组；子进程继承单实例锁，避免父进程退出后重叠处理。重启回收未完成作业，保留数据库、游标和审计。
- 后端记录运行源文件指纹；前端构建包含相同指纹，版本不一致时显示提示。两套本地预览使用独立会话 cookie，避免跨端口互相退出。
- 显示延迟只在相关内容可见且渲染后回执；摘要与片段证据面板分别计时，不代表操作员 PC 显示。缺失测量保持证据不足。
- 修复手机工具条中文竖向换行；复核抽屉支持键盘焦点和 Escape 返回。模式切换不重新挂载视频。
- 复现并修复离开挂起页面后启动轮询定时器，以及历史请求失败产生未处理异常。历史失败现在提供重试并保留已选证据。
- 不支持的视频编码不再错误地进入播放状态。显示读取/解码失败，并提供重试与下载原始证据。

## 已完成的验证

后端 **337 passed**，流水线与比较 **174 passed、1 skipped**，前端 **34 passed**，生产前端构建成功。日志保存在证据目录。后端警告为现有 FastAPI `on_event` 弃用提醒，尚未迁移生命周期 API。

```bash
cd /home/tianyu/Projects/QualityCheck
.venv/bin/python -m pytest tests -q
npm test
npm run build

cd /home/tianyu/Projects/DataPipeline
datapipeline-env/bin/python -m pytest QA_Pipeline/tests tests/test_comparison_workflow.py -q
```

覆盖指定审核员权限、旧证据与输入变化、并发决定、提醒重试与取消、反复标定、最新连续三条有效恢复证据、恢复后复发、重复/乱序收件、替代作业、225 条以上收件及嵌套布局、队列分页计数、事件游标恢复、轮询与回执重试、挂起请求恢复。完整导出测试验证超过 200 条事件不丢失且不包含认证表。

实际两个 localhost 预览分别带着打开的 SSE 连接退出，约 5.2 秒完成受控停机；Uvicorn 到达 5 秒宽限时取消长连接，然后执行应用退出。只重启已核实的 localhost PID，未重播种合成样本。重启前后收件游标、完成作业计数一致，运行指纹与新构建一致。另一个隔离测试实际启动临时子进程和孙进程，验证退出后没有活动进程、作业进入重试、重新取得锁后只完成一次。该测试不运行真实 NAS 检查，也不构成断电演练。

浏览器检查两个路由在 1440×900、1366×768、1024×768、390×844 下没有页面水平溢出，截图尺寸另行核对。确认中文长搜索词、空结果、加载、不可播放证据、可见键盘焦点、抽屉 Escape 关闭与焦点返回。已选持续漂移片段、9.933333 秒暂停位置、未完成中文复核依据在切换模式和整页刷新后保持。挂起请求与历史错误由自动化回归模拟，不能冒充真实网络故障演练。

后续浏览器自动化的部分导航、点击和补拍操作发生工具超时；随后 DOM 读取与 localhost API 仍可响应，原因尚未确定。要求的八张尺寸截图已经保存。最终构建脚本路径也在浏览器中核对，但这些检查不是“永不冻结”的证明。临时视口覆盖已恢复，用户预览页保留。

轨迹空闲渲染测试验证暂停时 100 次帧回调只绘制一次、交互后重绘、隐藏时不绘制。没有把此前 ChatGPT 中断归因于 QualityCheck 或此渲染逻辑。

## 明确未通过 / 尚未证明

1. **实时验收未开始。** 上游 SQLite 可读取，但监听心跳和最新收件仍停在 2026-09-28 13:30:13（中国时间），源游标 33177。未冻结两台试运行设备、没有 30 条新的实时收件；连续覆盖和实时延迟无法判定。需要现有上游服务恢复活动。本次没有启动或修改上游监听器。
2. **真实历史检查有证据缺口。** 两条历史录制均缺任务设备参考信息；其中一条还有 `consecutive_drops_outlier` 与不完整轨迹证据。没有将它们误清为通过。需要可核验的参考信息 / 完整输入后重新检查。
3. **真实视频在当前浏览器无法解码。** `ffprobe` 确认示例为 HEVC / `hev1`，640×480；localhost Range 请求返回有效的 HTTP 206 数据。轨迹可展示，原始视频需支持该编码的播放器，或另行实现清楚标注的本地兼容副本。没有改动 NAS 原文件，也没有用合成视频代替真实视频。真实视频播放验收未通过。
4. **准确率与竞争力未证明。** 200 条候选、0 条确认标签，20 个任务没有人工耗时。开发样本不独立。界面对照原版固定为 `07b61383650120cd0c48340dcc3ab55be6bcfe84`；检测器比较是同一代码的灵敏度 1.0 与 0.5。缺失历史检测器的重建能力没有被夸大。提示减少、零重大漏检和复核效率目标仍待人工验证。
5. **机器人执行和真实 PC 通知未验证。** 本地通知演示与生产传输隔离，应用与传输开关均关闭。任何部署、真实投递或机器人动作仍需单独明确授权。

## 备份边界

更改之前，三个项目的当前源文件（含嵌套项目、非忽略未跟踪文件）已递归保存在 GitHub 和 Gitea 的 `codex/backup-focused-review-resume-20260928` 非部署分支。两端均独立拉取并逐文件验证恢复，清单见 [备份证明](/home/tianyu/Projects/DataPipeline/docs/resume-review-backup-20260928.json)。忽略的运行数据库、缓存与凭证不属于源代码快照；本次重启保留它们。最早备份的嵌套 gitlink 缺口仍见 [早期缺口说明](/home/tianyu/Projects/DataPipeline/docs/trajectory-backup-recovery.md)，不声称能恢复当时已遗失的内容。

## 截图

截图目录：`/home/tianyu/Projects/QualityCheck/docs/evidence/focused-review/screenshots/`。

- `episodes-{1440x900,1366x768,1024x768,390x844}.jpg`
- `trajectory-{1440x900,1366x768,1024x768,390x844}.jpg`
- `trajectory-detailed-1440x900.jpg`：详细模式。
- `empty-state-1024x768.jpg`：长中文搜索与空结果。
- `historical-pending-1440x900.jpg`：真实历史待检查与视频兼容问题。
- `acceptance-real-labeling-1440x900.jpg`：真实验收与人工标注入口。
