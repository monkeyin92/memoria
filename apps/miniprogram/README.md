# Memoria 微信小程序

这是独立的原生微信小程序客户端；不复用或改写 `apps/h5/`。语音媒体通过受限 WSS 网关
进入既有 Cascade Agent，客户端只拿到短期 gateway ticket，不会接触 LiveKit participant
token、LiveKit API secret 或其他服务端密钥。

## 开发

1. 用微信开发者工具导入 `apps/miniprogram/`。
2. 将 `project.config.example.json` 复制为 `project.config.json`，再把其中的 `touristappid`
   替换为已备案的小程序 AppID；不要提交本机项目配置、上传私钥或证书。AppID 不是密钥，项目内
   上传脚本会固定经过审阅的 Memoria AppID，防止把源码误传到另一个合法微信项目。
3. 按环境调整 `config.js` 的 Control API HTTPS 地址。服务端生成的 gateway WSS 地址来自
   `MINIPROGRAM_MEDIA_GATEWAY_URL`，不是由小程序拼接。
4. 在微信公众平台配置 request、downloadFile/media 和 socket 合法域名。当前生产候选分别是
   `https://aigcnice.com:8443`（请求和头像）与
   `wss://aigcnice.com:8443/memoria-mini-media/v1/mini-program/media`；以实际备案域名、
   TLS 证书和后台审核结果为准。
5. 在开发者工具的隐私与权限配置中声明麦克风用途，并在真机允许 `scope.record`。

## 设备绑定与主体切换（多主体整改工作包）

- 首次绑定走 `pages/bind/index` 四分流：给孩子 / 给自己 / 给父母 / 家庭共同使用。
  请求体严格对齐整改文档 §9.1 `POST /v1/device-bindings`：只包含合同字段，
  客户端不提交 `policy_version`、`*_accepted` 等“假授权”字段（`utils/device-binding.js`
  的 `buildBindingRequest` 在发送前 fail-closed 校验）。
- 按场景采集最小必要信息；数字自我、声音复刻、传承与原始音频等敏感授权默认不勾选；
  “父母本人接受”类 consent 只展示为待确认提示，客户端不能代为提交。
- `pages/device/index` 展示绑定清单（版本化 manifest）与当前使用者：家庭模式或低置信度
  时由服务端 `POST /v1/sessions/resolve-subject` 提供候选主体，应用内确认走
  `POST /v1/sessions/{session_id}/active-subject`；`unknown_safe` 显示可解释降级。
- 敏感入口（数字分身、主人声纹、成长小结、原始语音授权、私人回顾）由
  `GET /v1/devices/{device_id}/runtime-profile` 的 `capabilities` 驱动，客户端不本地
  推断年龄。
- Runtime Profile 按 `RuntimeProfileSigned` canonical wire 形状严格校验（缺失/额外字段、
  过期、日历真实性（2 月 30 日/非闰年 2 月 29/24:00/越界 offset 一律拒绝）、签名信封
  形状、未知枚举、非法关系、unconfirmed/unknown 携带敏感能力一律 fail closed 为
  `unknown_safe` + 空敏感能力）。客户端不持有 HMAC 密钥，只做 TLS 响应边界 + 结构
  校验，不验证签名值；`unknown_safe` 只允许 `chat`/`english_practice` 且必须携带
  `DO_NOT_PERSIST` 义务，不允许 `tutor` 或任何敏感能力。minor 主体携带文档明确的
  minor-forbidden 能力（`voice_clone_use`/`digital_self_preview`/
  `legacy_grant_create`/`device_ownership_transfer`）或成人/适老服务模式
  （`adult_companion`/`adult_archive`/`self_preview`/`legacy_access`/
  `senior_companion`）整体 fail closed。
- **客户端只做结构/cross-invariant 防御，不自行扩展产品权限矩阵**：§4.2 中儿童明确可用的
  能力（声纹识别用于主体分流、长期个人记忆分项同意后召回、guardian_summary 的
  actor=guardian 场景）以及“默认禁用或需额外确认”的能力（payment/raw_audio/
  model_training）不由客户端结构层冻结——只要服务端权威签名 profile 签发并携带义务，
  客户端即接受；最终允许/拒绝仍以服务端签名 profile 为准，客户端只在入口层按
  capability 收敛展示。
- 本地缓存绑定 `device_id + binding_id/binding_version + session_id`，读取时四者必须
  完全匹配；同一会话 `session_epoch` 单调，回退写入被拒绝；解绑、登出或 binding 版本
  变化都会清理旧 profile。refresh/switch 按 device+session 上下文做请求代次隔离，
  晚到响应丢弃；普通 refresh 只接受幂等重放（同 session+epoch+canonical 全 24 字段
  + signature 完全一致），`setActiveSubject` 只接受 epoch 严格提升的新 profile。
  响应还必须与当前经过校验的 BindingManifest 上下文（device/binding/version/session）
  一致，任何 mismatch 拒绝并清理缓存；登出、重新绑定与 401 会清空内存代次/epoch
  守卫状态。
- BindingManifest 本地写入与每次读取都按 canonical 23 字段严格重验（枚举/版本/ID/
  时间/角色数组），被篡改的清单会被清理并视为未绑定（no_binding），不凭部分字段
  开放任何能力。
- 所有敏感页面与动作（数字分身、声纹录制/提交、成长小结读取/确认/授权、原始语音
  授权/撤回、私人回顾读取/生成、我的页统计）在每次执行前都重新通过
  `requireRuntimeCapability` 门禁；Profile 不可用时拒绝并给出可解释提示，不降级成人。
- **配置动作临时边界（P1）**：监护关系创建/授权修改、原始语音授权授予/撤回、声纹档案
  提交属于“配置能力”动作，单独走 `configActionGate` seam——服务端 consent/policy
  决策接口接入前一律 fail-closed 并说明“接口尚未接入”，不使用使用类能力门禁（避免
  首次设置死锁），也不在客户端用年龄绕过。这四条流程当前**不宣称**真实 E2E：绑定与
  主体解析/切换以服务端 multi-subject 接口为准，监护/consent/声纹配置待后端接入后由
  seam 消费服务端决策。
- `createMiniProgramSession` 会把经过校验的 BindingManifest `device_id` 写入
  `client.device_id`，供后续 `/session-policy` 关联 Binding/Runtime Profile；无绑定时
  显式声明 `client.session_scope: "unknown_safe"`，服务端不得按账号成人能力放开。
- 未成年年龄带统一使用 canonical `14_17`（不提交 `14_to_17`）；解绑/转赠后端尚无
  endpoint，客户端不提供伪成功入口。
- 多主体枚举、默认值与 producer lifecycle guard 由 canonical schema 确定性生成到
  `generated/multi-subject-contracts.js`；`utils/multi-subject-contracts.js` 只是兼容现有
  import 的薄转发，不维护第二份取值。生成器 `--check` 与 Node 测试共同阻止漂移；后端
  绑定/解析/切换接口尚未就绪时，页面以可解释错误呈现，不伪装成功。

小程序启动后不强制登录：陪伴、回顾和“我的”三个 Tab 均可先浏览。游客态不创建服务端
匿名账号，也不读取个人资料、统计或回顾；开始对话、生成回顾或修改资料时才进入微信登录。

本地只保存短期 access token、到期时间和最小身份快照，不保存长效 refresh token。token
到期后，已建立过身份的用户通过 `wx.login` 静默恢复；首次登录使用微信手机号授权，并可
主动选择微信昵称和头像。退出登录会同时终止隐藏陪伴页的媒体连接并清空当前字幕。

小程序采用受控话轮：AI 思考或播放期间暂停 `RecorderManager` 上行，不提供口头打断；
播放期可点“停止播放”立即淡出并取消当前 generation，但按钮不会同时打开麦克风。只有 Agent
重新允许输入、本地最后一段 WebAudio 已实际结束且尾音保护到期后，才恢复录音。用户主动
静音优先，回答结束不会擅自重新打开麦克风。

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
