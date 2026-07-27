# 项目交接

## 当前状态

- 生产 runtime：`20260727-170628`，source commit
  `8ef285ab4f6cc522974ed666d2781d877dd72dd7`，于
  `2026-07-27 17:47:06 CST` 激活。
- 生产 H5：`20260723-192611`，本轮 runtime 发布没有切换 H5。
- 微信小程序体验版：`0.8.51`，于 `2026-07-27 17:48:27 CST` 上传，
  包体 `607,643` 字节；未提交审核、未正式发布。
- 当前交付客户端为 `apps/h5` 与 `apps/miniprogram`；legacy Web 与原生 iOS 源码已移除。
- 历史路线、架构决策和发布证据分别保留在
  `docs/silicon-life-implementation-plan.md`、`docs/adr/` 与 `docs/releases/`。

## 最新实现

- `UtteranceRouter` 仍是 enroll、纯打断、打断后继续提问和普通聊天的唯一副作用入口。
- 小程序媒体契约由 `packages/contracts/miniprogram-media.json` 同时约束 Python 与
  JavaScript，固定下行 `24 kHz / mono / s16le / 20 ms / 960 bytes`。
- Socket 断开立即停止录音；mic 关闭压过迟到的 `RecorderManager.onStart`；同步发送失败
  不提交 sequence。
- 播放器使用固定帧校验、首批 lead、短 gain ramp 和 generation/reset barrier。
- 系统录音中断结束后发送 `uplink_discontinuity(next_sequence)`，同一 WSS 会话重置
  半帧、sequence 与 AEC 时序后恢复。
- Agent、H5 与小程序只消费带有效 `session_id`、generation 与权威来源的会话事件。

## 生产健康

- `agent / control-api / speaker-model / miniprogram-gateway` 四容器均为
  `healthy`、restart 0。
- readiness 为 `ready / 20260727-170628`；9/9 core checks、Agent heartbeat、
  LiveKit、FunASR、Qwen、Doubao 与 InterruptSemantic 均通过。
- 公网根 H5、兼容 H5、SPA、API live/ready 与 WMS 为 200；
  `/memoria-api/internal/` 为 404。
- PostgreSQL、MinIO、LiveKit、WMS、数据库快照与 Docker 数据卷未在清理中修改。

## 保留版本与回滚

- 当前 runtime：`20260727-170628`。
- 直接回滚 runtime：`20260727-120448`。
- 固定 H5：`20260723-192611`。
- 本机和生产均只保留当前与直接回滚两套 runtime 的四角色 Docker tag。
- 生产 source release 只保留 `20260727-170628` 与 `20260727-120448`；
  H5 release 只保留 `20260723-192611`。
- 回滚证据：
  `/var/backups/memoria/runtime-switch-20260727-170628-from-20260727-120448-20260727-174310/`。
- SQLite 快照 SHA-256：
  `50900119d0238b0cea55ceec182382ed841eaa1434c96e675f7daa43d2ff5430`。
- PostgreSQL custom dump SHA-256：
  `edba17f5e9ee7882d1966c7005c105f0923bb6aa047936602a55f051eb0e52db`。
- runtime 回滚不自动恢复数据库；只有数据迁移或数据异常时才使用快照。

## 2026-07-27 清理结果

- 删除非交付 `apps/web`、`infra/Dockerfile.web` 与对应 pnpm workspace 文件，
  约减少 `5,183` 行 tracked 内容。
- 删除三个干净临时 worktree、已合并 hotfix 分支、所有本机 Memoria 临时
  release/artifact、旧构建产物与测试缓存；文件系统回收约 `15.4 GiB`。
- 生产删除全部已导入 incoming 包、五套旧 source release、非当前 H5 候选、旧 artifact
  backup、旧 runtime-switch 目录和退出的 MinIO 初始化容器；文件系统回收约 `16.9 GiB`。
- 生产根分区约 `28 GiB` 已用、`86 GiB` 可用，使用率 `25%`。
- 本机和生产各删除五套旧 runtime 的 20 个 Memoria Docker tag。
- 没有运行 `docker system prune -a`；没有删除卷、数据库、其他项目镜像或跨项目构建缓存。

## 验证

- Ruff：通过。
- `mypy services --strict`：158 个 source files 无问题。
- Python：`1282 passed, 27 skipped`。
- H5：`232/232`，production build 通过。
- 微信小程序：`46/46`，全部 JavaScript syntax check 通过。
- `scripts/run_e2e.py --profile offline`：通过。
- 关键覆盖率子门槛：Agent orchestration `92%`、provider protocols `92%`。
- 既有全 `services` 覆盖率门槛仍未闭环：实测 `81.40%`，低于 CI 配置的 `85%`；
  本轮没有降低门槛或伪报通过。

## 未闭环与下一步

1. 用 `0.8.51` 在 iOS/Android 外放、听筒、蓝牙、系统录音中断和弱网下做真实声学验收。
2. 重点验证“等一下”“停一下，你叫什么名字？”、引用助手原话、噪声误触发、
   中断后“继续”和 1–2 秒句中停顿。
3. 若失败，先导出同一会话的 Agent/Gateway/Control 脱敏日志，再决定修复或回滚到
   `20260727-120448`。
4. 单独处理全仓覆盖率门槛：优先补齐 PostgreSQL/外部边界测试，不通过降低标准换绿。

## 用户工作区边界

以下未跟踪内容属于用户，必须保留：

- `.workbuddy/`
- `apps/miniprogram/assets/bg/aurora-light.webp`
- `apps/miniprogram/assets/mascot-alpha.webp`
- `apps/miniprogram/design-preview/`
