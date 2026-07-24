# Memoria 微信小程序

这是独立的原生微信小程序客户端；不复用或改写 `apps/h5/`。语音媒体通过受限 WSS 网关
进入既有 Cascade Agent，客户端只拿到短期 gateway ticket，不会接触 LiveKit participant
token、LiveKit API secret 或其他服务端密钥。

## 开发

1. 用微信开发者工具导入 `apps/miniprogram/`。
2. 将 `project.config.example.json` 复制为 `project.config.json`，再把其中的 `touristappid`
   替换为已备案的小程序 AppID；不要提交真实 AppID、密钥或证书。
3. 按环境调整 `config.js` 的 Control API HTTPS 地址。服务端生成的 gateway WSS 地址来自
   `MINIPROGRAM_MEDIA_GATEWAY_URL`，不是由小程序拼接。
4. 在微信公众平台配置请求合法域名和 socket 合法域名。当前生产候选分别是
   `https://aigcnice.com:8443` 与
   `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`；以实际备案域名、
   TLS 证书和后台审核结果为准。
5. 在开发者工具的隐私与权限配置中声明麦克风用途，并在真机允许 `scope.record`。

小程序只把身份快照放入本地存储；access token 仅保留在进程内。因此完全退出后需要重新
登录或开始匿名体验。这是有意保守的首版边界，不把长效 refresh token 写入小程序存储。

## 本地检查

```bash
npm --prefix apps/miniprogram test
find apps/miniprogram -name '*.js' -not -path '*/node_modules/*' -print0 \
  | xargs -0 -n1 node --check
```

## 发布前真机门禁

- iOS 与 Android 各至少一台；听筒、扬声器、蓝牙耳机分别验证。
- 验证播放期间录音不会把 Agent 音频回灌为用户输入或错误打断。
- 验证首次连接、长时间连续播放、弱网、网络切换、前后台、来电/微信语音打断与 ticket
  刷新重连。
- 在真实设备上确认 request/socket 合法域名、TLS、隐私声明和麦克风授权均通过。

在这些门禁完成前，不得将本客户端描述为已经完成生产全双工验收。
