# 项目交接

## 当前状态（2026-08-08）

- 生产 runtime/H5 均为 `20260808-171749`，源码 commit
  `9812fac155ef4f46a74d0d8dbfaf197fe9c5fa5a`；直接回滚目标为 `20260807-163916`。完整证据见
  `docs/releases/20260808-171749.md`。
- 四个应用容器及 PostgreSQL/MinIO 均 healthy；真实 LiveKit、QwenRealtimeSearch、DeepSeek、
  Doubao、FunASR、InterruptSemantic smoke 通过，readiness 绑定新 tag，core 10/10 ready。
- PostgreSQL 已 forward-only 安装独立 `memoria_evolution` 角色、8 张表、8/8 FORCE RLS 与 8/8
  controller policy。不要为代码回滚删除这些对象；旧 runtime 可与 additive schema 共存。
- H5 已最后切流；240 个 immutable URL 全部 HTTPS 200，历史 4776 条资源引用均可用。公网正向路由
  为 200，internal/PocketSparks/Goods Invoice 为 404；390x844 与 667x375 浏览器无横向溢出、
  console warning/error 为 0。
- 小程序 `0.8.66` 已于 `2026-08-08 19:21 CST` 经产品负责人授权使用微信开发者工具官方 CLI
  上传成功；AppID `wx20a3a044b52fcbb7`，包体 `656904 bytes（641.5 KB）`，说明为
  “绑定助手表情完整话轮 fence”。只创建开发/体验版，未提审、未正式发布。
- 项目内 `upload:test` 仍是长期可复现链路：Node 25/Web Storage 问题已 fail-fast，Node 24.16
  compile-only 通过；真实 CI 上传仍需把 `112.20.18.77` 加入微信代码上传 IP 白名单。本次 DevTools
  上传来自干净远端一致 HEAD `05a933f`，小程序业务源码相对 runtime tag `9812fac` 无变化。

## 回滚与备份

- 可执行回滚 receipt：`/var/backups/memoria/rollback-20260808-171749.env`，`root:root 0600`；它由
  上线前冻结的 legacy receipt 安全转换，未从切流后的 `current` 反推旧版本。原
  `release-state-pre-20260808-171749` 继续保留。
- H5 回滚 snapshot：`/var/www/memoria-releases/rollback-20260808-171749`，使用旧入口/provenance
  与当前 240 个 immutable assets；manifest SHA-256：
  `4f37ea9952463ef556aaf28e241c9d5244acf6a1236a46f49a5b968702890169`，逐文件验签与
  `www-data` 可读门禁通过。
- SQLite 双副本 SHA-256：`6443bde9695697296413ed9d7486b744bc52bc52162670d158529bbf4d1577af`。
- PostgreSQL dump SHA-256：`209b09028fdc31be2f2d5d8763b3fe456d34a625c8c43777827c815875cb3fdf`；
  MinIO 对象清单 SHA-256：`c7e273671460428a5fa9183841134a2eae6ded9814c0aeffab3fb6d5ee6155ff`。
- 旧 PostgreSQL/Control/Agent/Speaker/Gateway env 均已 root-only 备份。Runtime 故障先恢复旧 data
  Compose 与全部 env，再切旧软链和四应用；H5-only 故障只切 H5。

## 仍需完成

- 后续为恢复项目脚本真实上传，可把 `112.20.18.77` 加入微信代码上传 IP 白名单；这不再阻塞
  `0.8.66` 体验版交付。
- `0.8.66` 仍需在真实 iOS/Android 上覆盖麦克风、扬声器/AEC、弱网、前后台与蓝牙；H5 仍需真实
  登录后语音验收。现有单测、浏览器、WSS、Provider smoke 或上传成功都不能替代。
- 成功上传并确认回滚工件后，精确清理旧 incoming 大归档；保留当前 `20260808-171749` basis、
  `20260807-163916` runtime/H5/镜像以及全部备份。
