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
4. 在微信公众平台配置 request、downloadFile/media 和 socket 合法域名。当前生产候选分别是
   `https://aigcnice.com:8443`（请求和头像）与
   `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`；以实际备案域名、
   TLS 证书和后台审核结果为准。
5. 在开发者工具的隐私与权限配置中声明麦克风用途，并在真机允许 `scope.record`。

小程序启动后不强制登录：陪伴、回顾和“我的”三个 Tab 均可先浏览。游客态不创建服务端
匿名账号，也不读取个人资料、统计或回顾；开始对话、生成回顾或修改资料时才进入微信登录。

本地只保存短期 access token、到期时间和最小身份快照，不保存长效 refresh token。token
到期后，已建立过身份的用户通过 `wx.login` 静默恢复；首次登录使用微信手机号授权，并可
主动选择微信昵称和头像。退出登录会同时终止隐藏陪伴页的媒体连接并清空当前字幕。

小程序采用受控话轮：AI 思考或播放期间暂停 `RecorderManager` 上行，并显示“请等回应结束”；
不提供按钮或口头打断。只有 Agent 已进入可听状态、且本地最后一段 WebAudio 已实际结束后，
才恢复录音。用户主动静音优先，回答结束不会擅自重新打开麦克风。

H5 保持原有 LiveKit/WebRTC 可打断链路；“等等、等一下、停一下、先别说”等表达继续由
H5 传输层与服务端 `UtteranceRouter` 按控制语意处理。小程序与 H5 的交互策略不同，但仍
共用同一 Cascade Agent、权限、记忆和归档边界。

录音被系统打断时，小程序不会继续向旧 Socket 发送 PCM。系统恢复后会在同一会话发送
`uplink_discontinuity`，让网关清空半帧和 AEC 时序，再受控重启录音；恢复失败才提示用户
轻触恢复语音。

## 本地检查

```bash
npm --prefix apps/miniprogram test
find apps/miniprogram -name '*.js' -not -path '*/node_modules/*' -print0 \
  | xargs -0 -n1 node --check
```

EchoLife 用户迁移先执行只读 dry-run：

```bash
uv run python scripts/migrate_echolife_users.py \
  --source /path/to/echolife/server/data \
  --target-db /path/to/memoria.sqlite3 \
  --avatar-root /path/to/echolife/server/uploads/avatars \
  --public-base-url https://example.com/memoria-api \
  --dry-run
```

脚本兼容 EchoLife JSON 会话、备份和 `echolife.sqlite`，正式写入前会备份现有目标 SQLite。
旧故事、时间线和访谈记录只统计为 deferred，不会伪造成 Memoria 对话。

## 发布前真机门禁

- iOS 与 Android 各至少一台；听筒、扬声器、蓝牙耳机分别验证。
- 验证 AI 思考和播放期间没有上行 PCM，口头控制词不会停止当前回答或进入下一轮。
- 验证 Agent 回到 listening 后仍等待本地最后一个 source 结束，再恢复录音。
- 验证用户手动静音后，回答结束不会自动恢复；重新打开后下一轮录音正常。
- 验证首次连接、长时间连续播放、弱网、网络切换、前后台、来电/微信语音打断与 ticket
  刷新重连；系统中断结束后确认录音恢复且不混入中断前半帧。
- 在真实设备上确认 request/socket 合法域名、TLS、隐私声明和麦克风授权均通过。

在这些门禁完成前，不得将本客户端描述为已经完成生产受控话轮验收；小程序当前也不得
宣传为可随时打断的全双工语音。
