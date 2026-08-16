# Memoria 微信小程序

微信小程序是**机器人控制台、家庭账户入口和长期信息查看器**（整改方案 §2.1），
不是实时语音客户端：

- 不采集麦克风、不声明 `scope.record`、不创建任何录音器；
- 不播放实时 TTS、不建立媒体 WSS、不加入 LiveKit 房间、不接触设备媒体票据；
- ESP32-S3 机器人是**唯一面向用户的实时语音终端**；初始化完成后小程序可以关闭，
  手机离开局域网、小程序掉线都不影响机器人继续对话；
- 小程序只通过 HTTPS 访问 Control API，展示服务端权威状态，不伪装成实时音频状态机。

未通过 AEC / 双讲真机验收前，本客户端与产品文案不得宣称“全双工”（§7.2 / §9.5）。

## 开发

1. 用微信开发者工具导入 `apps/miniprogram/`。
2. 将 `project.config.example.json` 复制为 `project.config.json`，再把其中的
   `touristappid` 替换为已备案的小程序 AppID；不要提交本机项目配置、上传私钥或证书。
3. 按环境调整 `config.js` 的 `CONTROL_API_BASE_URL`（HTTPS）。小程序不拼接、不持有
   任何媒体 Gateway URL 或票据。
4. 在微信公众平台配置 request 合法域名（生产候选 `https://aigcnice.com:8443`，以实际
   备案域名、TLS 证书和后台审核结果为准）。**不需要** socket/媒体域名，**不需要**在
   隐私与权限中声明麦克风用途——`app.json` 不包含 `permission` /
   `requiredPrivateInfos`，微信后台权限列表应无麦克风权限（§5.2-1）。

## 页面与能力

- **首页**：机器人在线状态、当前 Soul/Persona、主要使用者、今日概览、家长提醒、
  快捷设置（勿扰/夜间/音量/学习模式）；不提供开始语音/实时文字会话入口。
- **设备**：机器人状态、绑定信息、当前使用者确认、设备设置（音量、屏幕亮度、
  音频模式、唤醒方式、允许的打断方式）、诊断信息（服务端权威字段）、敏感能力入口。
- **回顾**：会话记录与总结查询（HTTP），只消费权威持久化记录。
- **我的**：个人信息、敏感能力入口与授权状态；主人声纹仅展示服务端授权状态，
  **说话人登记在机器人端完成**，手机不再采集声纹。
- **启用流程**：扫码 → BLE/SoftAP 配网 → Claim → Binding → Activation；Wi-Fi 密码只
  保存在页面内存，Claim 一次性消费，绑定清单版本化可恢复。

### 设备设置（版本化）

所有设置走 `PATCH /v1/devices/{device_id}/settings` 的
`expected_settings_version` 版本化流程（`utils/api.js` 的 `updateDeviceSettings`）：
服务端确认前不保留本地假状态，冲突（409）时回退到最后一个权威版本并提示刷新。

- 字段合同与服务端 `DeviceSettings` 一致：`volume_limit`、`screen_brightness`
  （0-100）、`night_mode`、`do_not_disturb`、`learning_mode`、`audio_mode`、
  `wake_mode`、`allowed_barge_in`（非空去重列表）。
- 音频模式只提供 `diagnostics.allowed_audio_modes` 中服务端允许的取值；全双工
  （`full_duplex_verified`）只有在服务端声学能力登记通过 AEC 验收后才可能出现，
  未验收时不可选、不宣称。
- “语音打断”选项只在服务端登记 `aec_verified=true` 时展示；诊断不可用时音频模式与
  语音打断一律 fail-closed 关闭。

### 诊断信息（PR-18）

设备页消费 `GET /v1/devices/{device_id}/diagnostics/latest`，只展示服务端权威字段：
绑定版本/激活状态、Runtime Profile 版本、允许的音频模式、声学能力登记（板型、固件
范围、声学档案版本、AEC 验收、参考类型、最大打断等级、测试音量/距离）。连接质量
（丢包/欠载/重连）等字段由服务端补齐后才会展示，客户端不拼接、不虚构。

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
- 敏感入口（数字分身、成长小结、原始语音授权、私人回顾）由
  `GET /v1/devices/{device_id}/runtime-profile` 的 `capabilities` 驱动，客户端不本地
  推断年龄；手机声纹录取入口已按 PR-02 移除。
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
- 所有敏感页面与动作（数字分身、成长小结读取/确认/授权、原始语音授权/撤回、私人回顾
  读取/生成、我的页统计）在每次执行前都重新通过 `requireRuntimeCapability` 门禁；
  Profile 不可用时拒绝并给出可解释提示，不降级成人。
- **配置动作临时边界（P1）**：监护关系创建/授权修改、原始语音授权授予/撤回属于
  “配置能力”动作，单独走 `configActionGate` seam——服务端 consent/policy 决策接口
  接入前一律 fail-closed 并说明“接口尚未接入”，不使用使用类能力门禁（避免首次设置
  死锁），也不在客户端用年龄绕过。绑定与主体解析/切换以服务端 multi-subject 接口
  为准，监护/consent 配置待后端接入后由 seam 消费服务端决策。
- 未成年年龄带统一使用 canonical `14_17`（不提交 `14_to_17`）；解绑/转赠后端尚无
  完整 endpoint 时，客户端不提供伪成功入口。
- 多主体枚举、默认值与 producer lifecycle guard 由 canonical schema 确定性生成到
  `generated/multi-subject-contracts.js`；`utils/multi-subject-contracts.js` 只是兼容现有
  import 的薄转发，不维护第二份取值。生成器 `--check` 与 Node 测试共同阻止漂移。

小程序启动后不强制登录：首页、回顾和“我的”三个 Tab 均可先浏览。游客态不创建服务端
匿名账号，也不读取个人资料、统计或回顾；生成回顾或修改资料时才进入微信登录。

本地只保存短期 access token、到期时间和最小身份快照，不保存长效 refresh token。token
到期后，已建立过身份的用户通过 `wx.login` 静默恢复；首次登录使用微信手机号授权，并可
主动选择微信昵称和头像。

## 本地检查

```bash
npm --prefix apps/miniprogram test
find apps/miniprogram -name '*.js' -not -path '*/node_modules/*' -print0 \
  | xargs -0 -n1 node --check
```

静态门禁（`tests/no-realtime-media-gate.test.js`）保证生产包不得重新出现
`wx.getRecorderManager` / `RecorderManager` / `scope.record`、媒体 WSS、
媒体会话类、实时会话入口与已删除媒体模块。

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

## 发布前验收（§5.2）

1. 微信后台权限列表中无麦克风权限（必选/选填都没有）；
2. 冷启动、首页、回顾、设备页不创建任何录音器；
3. 不连接 MiniProgramMediaGateway 和 LiveKit（CI 静态门禁 + 生产包扫描）；
4. 关闭小程序后，机器人持续完成对话（真机/固件侧验收）；
5. 手机切换网络后，不中断机器人媒体会话（固件直连 Edge 的验收）；
6. 设置变更通过版本化 Runtime Profile 在设备下一安全点生效；
7. 小程序只显示权威持久化记录，不伪装成实时音频状态机。

在这些门禁完成前，不得将本客户端描述为已完成生产验收；小程序也不得宣传为可随时
打断的全双工语音。
