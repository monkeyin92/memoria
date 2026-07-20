# 项目交接

## 当前生产

- 唯一客户端是 H5，入口为 <https://aginice.cn:8443/>；runtime 为 `20260719-215553`，H5 热修版为 `20260720-095939`。
- 固定级联主链：FunASR Realtime + 百炼 Qwen + CosyVoice 3.5；不向用户暴露 Omni/iOS 路径。
- 三个应用容器 `agent / control-api / speaker-model` healthy；`/health/ready` 为 200，绑定 release `20260719-215553`、provider `qwen`，9 项 core check 全部 ready。
- 独立同机数据栈为 PostgreSQL 17 + pgvector 0.8.1 与 MinIO；不复用其他项目卷。32 张表全部 FORCE RLS，MinIO 两个 bucket 均启用版本控制。
- H5 直接回滚点为 `20260719-215553`；runtime 完整回滚点仍为 `20260719-000731`。旧 `/etc/memoria.env`、镜像和 release 目录已复核存在，发布前备份见 `/var/backups/memoria/`。

## 本轮发布

- H5 注册热修 release：[`docs/releases/20260720-095939.md`](docs/releases/20260720-095939.md)；runtime 正式 release 仍为 [`docs/releases/20260719-215553.md`](docs/releases/20260719-215553.md)。
- 匿名身份原地注册时只更新账号凭据，不再错误失效同一 `user_id` 已加载的 Profile；跨 `user_id` 登录仍保持准备页直到新 Profile 就绪。提交前完整门禁通过：Ruff、mypy、Python `533 passed / 21 skipped`、H5 100 项测试及 production build。
- 本机完成 `linux/amd64` 增量镜像与 H5 production build，压缩镜像归档 SHA-256 为 `a0dbba06fafa3d28279f71a77f0a605c082123cb50edf6c9938019fba2c02e79`；服务器只执行 `docker load` 与 `compose up --no-build`。
- readiness 权限边界已修正：Agent one-off 只校验 Agent env；Control API one-off 使用 control env 标记 smoke evidence。不要把 `MEMORIA_AUTH_SECRET` 放回 Agent env。
- SQLite 迁移 376 条事件，幂等复跑新增 0；联合恢复演练 21 张权威表与 376 条事件一致，RTO 2.052 秒。
- H5 `index / JS / CSS` 公网哈希与本机构建一致；桌面和 390x844 浏览器检查无横向溢出、图片失败或 console warning/error。
- 发布后修复本地 WebSocket mock 被 macOS 系统代理接管导致的 FunASR 并发测试误报；压力环 40/40、共享 mock 定向测试及全量 pytest 均通过。该修复只涉及测试代码，不需要生产 runtime 重启。
- 本机固定镜像 tag 已从服务器回补，后续本机构建保留在 `local-overwrite-20260719-215553`；三张镜像 RootFS/Config 双哈希与服务器一致。跨 Docker Desktop/Linux Engine 不以 `.Id` 相等作为一致性依据。

## 已交付能力

- P0.5：用户名/密码注册登录、稳定 `user_id`、匿名身份原地升级和跨账户隔离。
- P1-P3：追加式证据账本、加密 spool、人物/关系/时间线/人生知识、审核、全文与 pgvector 检索、迁移和恢复。
- P4-P5：证据化 Persona、实时胶囊、CosyVoice 3.5 声音授权、登记 saga、盲测、激活、撤销和供应商补偿。
- P2：`owner / guest / uncertain` 权限、固定 CAM++ ONNX 模型服务、shadow/版本 readiness、声纹治理和私人记忆硬门禁。
- P6：私人记忆实时接入、声音降级与时间戳门禁、导出、删除 fence、tombstone、联合恢复和原始语音独立授权。

## 关键边界

- CAM++ 没有 anti-spoof，当前声纹仍是 shadow-only；不能宣传为已完成生产主人识别。
- 生产未用授权真人样本完成 FAR/FRR/EER、unknown rejection、回放攻击和 CosyVoice 真人盲测，不能自动激活正式声纹模板或复刻声音。
- PostgreSQL、WAL archive、MinIO 与备份目前都在同一台服务器；虽有版本控制和恢复演练，但没有异地副本/KMS/PITR，不能承诺“永不丢失”。
- 真实手机的耳机、扬声器、噪声、重叠、回放和弱网全双工矩阵仍未完成。

## 下一步

1. 用至少 200 条明确授权真人录音完成声纹和抽取/检索指标报告。
2. 完成 CosyVoice 3.5 正式 enrollment、人格 A/B、声音相似度与自然度盲测。
3. 增加异地对象副本、独立备份目标与 PITR，再做跨环境联合恢复演练。
4. 用真实手机完成 H5 全双工听感、权限污染和弱网设备矩阵；原生 iOS 不在范围内。
