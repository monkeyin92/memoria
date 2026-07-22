# 项目交接

## 当前生产

- 唯一交付客户端为 H5：
  - <https://122.51.108.140:8443/>
  - <https://aigcnice.com:8443/>
- runtime/H5 当前 release 均为 `20260721-224804`；固定主链为
  FunASR Realtime → 百炼 Qwen → 豆包 Seed-TTS 2.0 双向流式 → LiveKit/H5。
- `agent / control-api / speaker-model` 均 healthy、restart 0；发布后 15 分钟日志
  error marker 为 0。readiness 绑定 `20260721-224804`，LLM `qwen`、TTS `doubao`，
  9/9 core checks ready。
- PostgreSQL 17 + pgvector、MinIO 与 LiveKit 继续独立同机运行；WMS 的 443、
  `/wms/` 和 `/wms/api/` 未改动。

## 本次热修：避免主人被误静音

- `20260721-212040` 把 shadow ambiguous 也当作非主人拒绝。真实问题会话有 4 个
  非空 ASR final，owner score 为 `0.420678 / 0.473613 / 0.437784 / 0.469538`，
  但 4/4 全被门禁拦截，导致 0 个提交话轮、LLM 或 TTS。线上先紧急回滚到
  runtime `20260721-181810`，再发布本修正版。
- Profile 字段 `reject_non_owner_voice` 仍默认 `true`；H5“我的 → 陪伴偏好”改为
  “过滤明显旁人（实验）”，不再把概率型声纹包装成“仅听主人”。
- 开启时只拒绝 formal `guest/owner_mismatch` 与明确的
  `shadow_guest_candidate`。shadow `shadow_ambiguous_candidate` 和 formal
  `ambiguous_score` 可普通聊天以避免误静音主人，但保持 non-owner/uncertain，
  `history_eligible=false`，不能获得私人记忆、工具、敏感权限或主人历史资格。
- Shadow guest cutoff 默认 `0.40`，分类取 Profile 已存阈值与运行时阈值的较小值。
  本次真实样本回放为主人 4/4 放行、孩子 7 段中 6 段拒绝；这只是止损矩阵，不是
  FAR/FRR/EER 或强身份认证结果。
- 关闭开关后访客可以聊天和打断，但仍不能升级 `owner` 或写入主人回顾。服务端明确
  拒绝时，H5 会给出可操作的内联提示，不再表现为无反馈。

## 滋滋声取证与保护

- 问题会话只有欢迎语进入播放；订阅、track 附着、播放和首包都成功，没有持续增长的
  packet loss/concealment。`packetsDiscarded=164` 在首个有效采样即存在且后续不增长，
  不能把累计值直接归因于当次播放丢包。
- H5 WebRTC 遥测已改为上报 received/lost/discarded packet、concealed sample 和
  total sample 的相邻采样增量。
- 豆包 PCM 流新增连续性守卫：跨 chunk 缓冲奇数字节边界，只输出完整 PCM16 sample；
  总长度为奇数时明确失败，并记录不含音频正文的 `doubao_pcm_summary`。
- 这些改动消除一个可验证的分片风险并补齐取证，但没有同 generation 的供应商原始
  PCM、浏览器接收流和设备外录前，不能宣称滋滋声已经闭环或归责豆包/WebRTC。

## 清理结果

- 已删除零引用 `QWEN_OMNI_MODEL` alias、H5 deprecated Omni helper、当前 Doubao
  runtime 不再消费的 CosyVoice settings/split-env 旧键，以及 Hydra `outputs/`。
- 新 source artifact 使用 Git clean 文件清单，排除 `__pycache__`、`.pyc`、本地 env、
  Node cache、数据和构建输出；H5 bundle secret-like 扫描通过。
- 新 H5 union 没有继承 164 个 macOS `._*` AppleDouble 元数据垃圾；保留 178 个真实
  新旧 immutable assets。CosyVoice 治理、Omni 隔离 A/B、迁移、ADR 与历史 release
  仍有回滚或取证价值，不误删。

## 验证与发布证据

- Python：全量 `uv run pytest -q` 退出码 0，`730 tests collected`；Ruff、strict
  mypy、`git diff --check` 通过。最近一次覆盖率证据为 `82.05%`，仍低于仓库 85%
  门槛，不能写成 CI 全绿。
- H5：`138/138`，production build 通过。
- 服务器候选 smoke：candidate H5、SPA、API、默认/关闭偏好与 SQLite 重启持久化通过。
- Provider：LiveKit、FunASR、Qwen、Doubao PCM/字时间戳/CancelSession 全通过；
  readiness 为 `20260721-224804`、9/9 core ready。
- 公网：IP/域名 root、H5、SPA、live/ready、新 JS/CSS、负向 internal 路由、Nginx 与
  WMS 共存通过；178 个真实 union assets 经 SNI loopback 逐项返回成功。
- 内置浏览器：390×844 与 667×375 无横向溢出，console 0 warning/error；未创建
  生产测试账号。当前 bundle 为 `index-B2cPI3-1.js` / `index-C6TCaVi0.css`。
- 发布记录：[`docs/releases/20260721-224804.md`](docs/releases/20260721-224804.md)。

## 回滚

- runtime：`20260721-181810`；回滚前恢复：
  - `/var/backups/memoria/memoria-control-api.env-pre-20260721-224804`
  - `/var/backups/memoria/memoria-agent.env-pre-20260721-224804`
- H5：`20260721-181810-rollback-union-20260721-212040`。
- SQLite 双快照：
  - `/var/lib/memoria/memoria-pre-20260721-224804.sqlite3`
  - `/var/backups/memoria/memoria-pre-20260721-224804.sqlite3`
- 旧 runtime 可忽略新增 SQLite 列；常规代码回滚不恢复数据库。恢复双 env 后必须以旧
  tag 显式重启 Compose 并刷新 readiness。

## 已知边界与下一步

1. 用真实手机复测主人、孩子、ambiguous、明确旁人、短句打断，以及开关开启/关闭两种
   状态；自动化不能替代这一步。
2. 同一 generation 同步采集豆包原始 PCM、浏览器远端 MediaStream 与设备外录，闭环
   滋滋声。
3. “（开心地笑着）”被朗读是 LLM 舞台指令原样进入 TTS；本 release 未把普通括号文本
   假装成笑声控制。需单独设计过滤与可验证的笑声降级策略。
4. 尚未完成 200 条授权录音 FAR/FRR/EER、回放攻击、耳机/扬声器、噪声和弱网矩阵；
   生产 PostgreSQL/MinIO 仍同机，无异地副本/KMS/PITR。
5. 当前发布来自本地 dirty worktree，Git `HEAD=411b2ea`，尚未提交或推送；生产工件以
   release artifact SHA-256 为复现依据。
