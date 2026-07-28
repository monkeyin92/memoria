# 小程序 WSS `connection refused` 诊断与候选修复

## 结论

2026-07-28 的问题不是网关宕机、gateway ticket、Agent、FunASR 或播放器错误。生产网关
healthy，Control API 当前下发的地址为：

```text
wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media
```

微信开发者工具已在此地址完成 `open → hello → ready`；生产公网 Upgrade 也返回 `101`，无效
ticket 会在握手后按协议关闭为 `4401`。因此 `connection refused` 发生在业务协议之前。

## 已验证与未采用的替代方案

- 8443：当前网络出口直连 TLS/WSS 成功，生产 Nginx、stream 预读、9443 HTTPS 虚拟主机和
  loopback gateway 均连通。
- 443：曾在 WMS TLS 虚拟主机增加精确的媒体代理路径；该路径在服务器侧能返回 `101`，但从
  当前网络出口直连时，客户端在收到 ServerHello/证书后立即发送 TCP RST。因此它不能作为这台
  设备网络的修复，配置已回撤，未改变 WMS 路由或生产下发地址。
- 现有 WSS location 关闭 access log、gateway 也关闭 Uvicorn access log；不能把“没有 HTTP
  access log”当作手机没有到达服务器的证据。下一次真机复现必须用 8443 的短时 tcpdump 关联。

## 候选代码修复

小程序仅在原始 SocketTask 错误包含 `connection refused` 时：

1. 关闭失败 socket，避免迟到 close 污染下一次连接；
2. 刷新同一业务 session 的 gateway ticket，不创建第二个语音会话；
3. 延迟 400 ms 后自动再连一次；
4. 第二次失败才显示可操作提示：关闭 VPN/代理后重试，或切换 Wi-Fi/移动网络。

域名白名单、TLS/证书、超时、网关关闭和票据错误不参与自动重试，避免掩盖配置问题。

## 验收与回滚

- 待上传体验版：在同一 iPhone 上依次验证 VPN/代理关闭后的 Wi-Fi、移动网络和原网络；若仍
  失败，在点击“开始语音陪伴”的 30 秒窗口抓取服务器 8443 SYN/TLS 包。
- 候选只修改小程序客户端；当前后端 runtime `20260727-235959`、H5 `20260723-192611` 不需
  切换。小程序体验版回滚即重新上传/指定前一体验版 `0.8.53`。
- 443 试验的 Nginx 与 Control API 环境文件均有 root-only 备份；生产最终配置已恢复到 8443，
  Nginx active、Control API readiness `ready`。
