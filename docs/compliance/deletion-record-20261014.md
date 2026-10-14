# 测试服务器数据删除记录（2026-10-14）

- 服务器：aginice.cn 测试服务器（ssh memoria-prod），release 20261014-subscribe-optout-v1
- 批准：Monkey In，2026-10-14 15:05（UTC+8）私聊回复「开始清理」
- 数据来源：自动语音测试，无真人儿童
- 执行：memoria技术部，所有时间为 UTC+8

## (a) 上行录音
| 删除内容 | 数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/lib/memoria-agent/diagnostics/pcm-tap/*.wav`（08-19 至 08-22） | 57 个 WAV，约 135M | 2026-10-14 15:08:47 | 0 个文件（目录 12K 空壳保留） |
| `/etc/memoria-agent.env` 中 `MEDIA_PCM_TAP_DIR=/tmp/media-pcm-tap` 及 `MEDIA_PCM_TAP_MAX_BYTES=4194304` | 2 行 | 2026-10-14 15:08:47 | 0 行；重建后的 bridge 容器内无 PCM_TAP 环境变量 |
| `/tmp/media-pcm-tap`（宿主机与容器内） | 原本就不存在，0 个 | 2026-10-14 15:08:47 | 不存在 |

- 修改前的 env 已复制为 `/etc/memoria-agent.env.pre-pcmtap-removal-20261014`（只有配置，没有录音）。
- 15:08:57 重建 voice-core-media-bridge；15:09:53 和 15:10:46 两次复查都是 healthy，近 2 分钟日志里没有 error 或 traceback。其他容器也都正常。

## (b) 旧数据库
| 删除内容 | message 行数 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/backups/memoria/migrations/20260720-140053/memoria.sqlite3`（含 -wal/-shm） | 380 | 2026-10-14 15:08:47 | 文件不存在 |
| `/var/lib/memoria-candidate-20260719-204618/memoria.sqlite3`（含 -wal/-shm） | 376 | 2026-10-14 15:08:47 | 文件不存在，目录为空 |

- `frozen-state.tar.zst` 见下方「追加删除」。

## (d) 现网 Postgres（memoria 库）派生数据
| 表 | 删除行数 | 时间 | 复查后剩余 |
|---|---|---|---|
| daily_summaries | 13 | 2026-10-14 15:08:57 | 0 |
| memory_claims | 4 | 2026-10-14 15:08:57 | 0 |
| life_episodes | 2 | 2026-10-14 15:08:57 | 0 |
| memory_search_documents | 8 | 2026-10-14 15:08:57 | 0 |
| memory_search_document_sources（外键级联） | 8 | 2026-10-14 15:08:57 | 0 |
| episode_evidence（外键级联） | 2 | 2026-10-14 15:08:57 | 0 |
| timeline_entries（外键级联） | 2 | 2026-10-14 15:08:57 | 0 |

- 临时备份：删除前在 15:08:47 用 pg_dump 导出上述 7 张表（data-only，28074 字节，root 600 权限），存放在 `/var/backups/memoria/derived-20261014/derived.sql`。服务确认稳定后，于 2026-10-14 15:10:46 删除，复查确认已不存在。
- 旧 SQLite 中的 daily_summaries 见下方「追加删除」。各 memoria.pre-*.sqlite3 文件里也各有 13 行，会随 (c) 一起处理。

## 追加删除（Monkey In 于 2026-10-14 15:12（UTC+8）私聊批准）
| 删除内容 | 数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| `/var/backups/memoria/migrations/20260720-140053/frozen-state.tar.zst`（2026-07-20 打包，含 2026-07-16 至 07-19 的旧 memoria-pre-*.sqlite3） | 1 个归档，7.0M，共 1604 个条目 | 2026-10-14 15:12:36 | 文件不存在，目录为空 |
| `/var/lib/memoria/memoria.sqlite3` 中 daily_summaries 表的行（只删行，文件保留给 (c) 使用） | 13 行 | 2026-10-14 15:12:48 | 0 行；文件仍在（2027520 字节），属主仍为 65532 |

- 删除后 control-api 与 bridge 都是 healthy。

## (c) 机器人回复文本（代码修复已部署后执行）
- 前提：PR #235（squash `5333d1a`，CI 通过）已合并，并按常规发布流程部署为 `20261014-child-playout-ephemeral-v1`。各步骤均 PASS：verify-load 16:38:32、freeze 16:38:45、env 16:39:27、schema 16:39:29、cutover 16:40:07、finish 16:41:25。
- 部署后：bridge 与 control-api 均 healthy，重启 0，revision 都是 5333d1a；外部 readiness 200 `ready`；bridge 中 `ENVIRONMENT=production`，`MEDIA_PCM_TAP_DIR` 为空；近 3 分钟日志无 error。修复相关的 4 个测试文件在发布 worktree 中全部通过。回滚目标是 `20261014-subscribe-optout-v1`。
- 部署前检查：`/etc/memoria-*.env` 和 `/etc/memoria-media-runtime/` 下都没有 MEDIA_PCM_TAP。

| 删除内容 | 方式与数量 | 时间 | 复查后剩余 |
|---|---|---|---|
| Postgres `archive_evidence_events` 中 `assistant.playout_stopped` 的回复文本 | 只从 payload 删掉 `text` 键，保留行和表结构（删行会级联到二十多张记忆表）。共 1,255 行带文本，另 7 行原本就没有文本，1,262 行全部保留 | 2026-10-14 16:34:25 | 1,262 行，带 `text` 的 0 行（16:43 复查） |
| agent 加密 spool `/var/lib/memoria-agent/archive-events.spool` | 384 行积压（未送达）事件，全部是 `assistant.playout_stopped` 且带文本（2026-09-30 至 10-13）。加锁后清空 | 2026-10-14 16:34:15 | 0 字节（部署后复查仍为 0） |
| 5 个旧切流 dump：`/opt/memoria/releases/{20261013-speaker-retire-v1, 20261014-night-window-v1, 20261014-night-default-2100-v1, 20261014-safety-subscribe-v1, 20261014-subscribe-optout-v1}/.cutover/memoria-pre-*.dump` | 5 个，每个都含 1,255 条带文本的回复 | 2026-10-14 16:42:44 | 不存在 |
| `/var/lib/memoria/memoria.pre-{上述 5 个 tag}.sqlite3`，含 -shm/-wal | 5 个库加 10 个附属文件，共 15 个（库内无回复文本，但每个有 13 行 daily_summaries） | 2026-10-14 16:42:44 | 不存在 |

- 新的回滚备份：先清掉文本，再运行 freeze，所以本次 freeze 生成的 `memoria-pre-20261014-child-playout-ephemeral-v1.dump`（3,924,871 B，16:38）中带文本的 playout 行为 0（已用 pg_restore 核对）。配套的 `memoria.pre-20261014-child-playout-ephemeral-v1.sqlite3` 中 daily_summaries 为 0。这两份作为当前唯一的回滚点保留。
- 全盘复查：除上面这一份新 dump 外，再没有 memoria 的 dump；`/opt/pocketsparks/backups` 属于共用主机上的另一个项目，没有动。
- 剩余事项：HANDOFF 中「备份已全删」一句需要加注更正（不删原句），由技术部在仓库中处理。注意现在仍保留一份不含文本的回滚 dump。
