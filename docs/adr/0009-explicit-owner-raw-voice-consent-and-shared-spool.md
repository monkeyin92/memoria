---
status: accepted
date: 2026-07-19
---

# 原始主人语音使用独立授权并复用档案加密 spool

Memoria 的转写是终身档案基础证据，原始语音则是更敏感、体积更大的可选资产。系统因此默认不保存原始语音；只有账户主人明确授予 `raw_voice_archive` 授权、当前话轮被 `SpeakerAuthority` 判为 `owner` 且 Control API 再次确认同一授权仍有效时，才保存 mono PCM16/16 kHz WAV。撤销授权会删除已有原始语音对象和 manifest，但保留转写、纠错链和结构化记忆。

转写与原始语音复用一个 `ArchiveSink`、一个 Fernet 加密 spool 和一把跨进程文件锁，通过 `target=event|raw_audio` envelope 分流。Agent 先提交转写，再尝试原始语音；音频的永久拒绝只删除对应 envelope，暂时失败则保留重试并继续处理后续转写。容量压力下，新转写可在同一把锁内驱逐所需数量的旧 `raw_audio` envelope，但不得驱逐 `event` 或旧版无 envelope 的转写行；backlog 只剩失败音频时，新转写仍先尝试直接交付。这样保留一套容量控制、加密、重放和幂等机制，同时明确转写高于可选音频的持久化优先级。

## Considered Options

- 默认保存所有话轮原始音频：可提供更多训练素材，但违背最小采集原则，也会把 guest、回声和未授权声音带入长期资产。
- 为音频建立第二个独立 spool：隔离最直观，但增加第二套密钥、锁、容量、告警和回放顺序；当前 envelope 已能让音频拒绝不影响转写。
- 只保存转写，永不保存原始音频：隐私最简单，但无法支持经授权的声音复刻、档案级复核和长期声学表达研究。
- 把 WAV 直接放入普通 evidence JSON：接口更少，但突破 64 KiB 证据边界，放大数据库与队列压力，也不利于对象生命周期治理。

## Consequences

- H5 提供独立授权、撤销和后果说明；账号登录不等于原始语音授权。
- Agent 只从冻结的 owner 话轮 PCM 生成 WAV；guest、uncertain、空音频、非法格式和授权查询失败都降级为只保存转写。
- 共享 spool 的容量优先级固定为转写高于原始音频；历史音频占满空间时先驱逐音频，不能让后来的权威转写因可选资产失去落盘机会。
- 回放允许越过暂时失败的 `raw_audio` 继续交付转写；只有权威转写自身暂时失败才停止越过并保持其顺序。
- Control API 从 `session_id` 解析账户并复核 grant；调用方不能指定账户。WAV 有应用层 2 MiB 上限、Nginx 4 MiB JSON 上限和独立限流。
- 对象写入与 PostgreSQL manifest 通过补偿式 saga 协调；冲突、授权竞态和请求取消会删除未提交对象。撤销在数据库事务内先冻结 grant 并取得精确对象快照，对象全部删除成功后才按快照 key 清理 manifest；失败保留 manifest 供幂等重试，避免并发上传形成无清单对象，也不会误删撤销后重新授权产生的新资产。
- 对象 `put` 成功但 manifest 事务提交前进程崩溃仍可能留下短暂孤儿，因此生产继续执行对象清单对账；这不改变 manifest 作为删除重试依据的规则。
- 旧版无 envelope spool 行继续按转写解释，避免升级时丢失已有证据。
