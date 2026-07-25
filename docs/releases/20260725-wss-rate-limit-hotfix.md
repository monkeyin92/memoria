# 小程序语音 WSS 限流热修

## 目标

修复小程序点击“开始语音陪伴”后偶发显示“语音网络连接失败”的问题。

## 根因

`/memoria-mini-media/v1/mini-program/media` 与登录、创建会话共用
`memoria_session` 限流桶（每 IP `10r/m`、`burst=3`）。同一 NAT IP 在短时间内
完成登录、建会话和重连时，媒体握手会在到达网关前被 Nginx 以 HTTP 429 拒绝；
微信 `SocketTask.onError` 将该响应笼统显示为网络失败。

## 改动

- 保留登录和 Control API 的既有限流。
- 为媒体 WebSocket 增加独立的 `memoria_media` 桶：`30r/m`、`burst=6`。
- 保留短期 ticket 鉴权、128 KiB 上限和访问日志脱敏。

## 验收与回滚

- 连续五次无效 ticket 握手必须都升级到 WebSocket 后由网关关闭为 `4401`，不得收到
  Nginx `429`。
- 使用真实短期 ticket 必须收到 `ready`。
- 仅安装两个 Nginx 配置文件并 reload；不重启 Agent、Control API、网关或 H5。
- 回滚时恢复本次安装前备份的 `/etc/nginx/conf.d/memoria-limits.conf` 和
  `/etc/nginx/snippets/memoria-https.conf`，通过 `nginx -t` 后 reload。

## 已执行结果

- 已备份生产配置至 `/var/backups/memoria/20260725-wss-rate-limit-hotfix`，`nginx -t`
  通过后完成 reload。
- 连续五次无效 ticket 均为 `4401`，无 `429`。
- 使用真实短期 ticket 收到 `ready`，耗时 422 ms。
- Control API readiness 仍为 `ready`，runtime 保持 `20260725-235824`；Agent、Control API
  和小程序网关均未重启。
