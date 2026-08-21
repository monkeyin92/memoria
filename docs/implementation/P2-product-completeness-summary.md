# P2 产品完整性功能 - 实施概要

本文档概括 P2-2 至 P2-7 的实施要点。详细设计参考整改方案第 5 节。

---

## P2-2: Soul/Persona 配置界面

**目标**: 让用户通过小程序配置机器人的人格特征

**关键功能**:
- 机器人名称设置
- 基础人格模板选择（友善/专业/活泼/严肃）
- 语气调节：主动程度、幽默度、解释深度
- 服务模式：儿童陪伴/成人助手/适老服务/家庭共享

**小程序页面**: `apps/miniprogram/pages/persona/edit.wxml`

**API 端点**:
```
GET /v1/devices/{device_id}/persona
PATCH /v1/devices/{device_id}/persona
```

**数据结构**:
```json
{
  "persona_id": "persona_xxx",
  "name": "小智",
  "base_template": "friendly_companion",
  "traits": {
    "proactiveness": 0.7,
    "humor_level": 0.5,
    "explanation_depth": "medium",
    "formality": 0.3
  },
  "service_mode": "child_companion",
  "version": 12
}
```

**验收标准**:
- ✅ 设置通过版本化 Runtime Profile 下发
- ✅ 设备在安全点生效（非对话中途）
- ✅ 页面明确 Soul 影响"怎么说"，Policy 决定"能不能做"

---

## P2-3: 家庭成员与监护设置

**目标**: 支持多人使用同一设备，区分不同家庭成员

**关键功能**:
- 家庭成员邀请与确认
- 说话人登记（声纹/面部识别）
- 主要使用者切换（服务端 `active_subject_id`）
- 未知说话人降级策略
- 儿童监护：可见范围、时长限制、夜间设置

**小程序页面**:
```
apps/miniprogram/pages/family/
├── members.wxml       # 成员列表
├── invite.wxml        # 邀请成员
└── child-guard.wxml   # 儿童监护设置
```

**API 端点**:
```
GET /v1/families/{family_id}/members
POST /v1/families/{family_id}/invitations
PATCH /v1/families/{family_id}/members/{member_id}/guardian-settings
```

**重要约束**（整改方案 5.1.D）:
- 主要使用者来自服务端 nullable `active_subject_id`
- 切换必须获得严格递增的 session epoch
- `active_subject_id=NULL` 表示 `unknown_safe` 模式
- 绑定负载区分：账号所有者、设备管理员、主要数据主体

**验收标准**:
- ✅ 成员切换触发新 session epoch
- ✅ 未知说话人降级策略生效
- ✅ 儿童监护时长和夜间限制生效

---

## P2-4: 设备管理功能

**目标**: 提供完整的设备生命周期管理

**关键功能**:
- 设备信息展示：固件版本、在线状态、最近心跳
- 设备设置：音量、亮度、勿扰模式、夜间模式
- 打断方式配置：按钮/停止词/语义打断
- 固件 OTA 升级/回滚
- 网络重配、解绑、转赠、恢复出厂

**小程序页面**:
```
apps/miniprogram/pages/device/
├── detail.wxml        # 设备详情
├── settings.wxml      # 设备设置
└── manage.wxml        # 高级管理（解绑/转赠）
```

**API 端点**:
```
GET /v1/devices/{device_id}
PATCH /v1/devices/{device_id}/settings
POST /v1/devices/{device_id}/ota-updates
POST /v1/devices/{device_id}/unbind-intents
POST /v1/devices/{device_id}/transfer-intents
```

**设备设置结构**:
```json
{
  "volume": 50,
  "brightness": 80,
  "do_not_disturb": false,
  "night_mode": {
    "enabled": true,
    "start_time": "22:00",
    "end_time": "07:00",
    "volume_limit": 30
  },
  "interruption": {
    "allowed_methods": ["button", "local_keyword", "semantic"],
    "keyword_sensitivity": "medium"
  },
  "version": 27
}
```

**验收标准**:
- ✅ 设置变更通过版本化 API 更新
- ✅ 固件版本、升级、回滚状态展示
- ✅ 物理静音状态只读展示（不可远程控制）

---

## P2-5: 回顾与总结功能

**目标**: 让用户查看和回顾对话历史

**关键功能**:
- 会话列表按主体和设备过滤
- 每次会话摘要
- 区分"实际听到"与"生成但未播放"
- 记忆候选与已确认记忆分开
- 日报、周报、学习进度
- 原始录音默认不开放（隐私保护）

**小程序页面**:
```
apps/miniprogram/pages/review/
├── conversations.wxml  # 会话列表
├── session.wxml        # 单次会话详情
├── memories.wxml       # 记忆管理
└── reports.wxml        # 日报/周报
```

**API 端点**:
```
GET /v1/conversations?subject_id=&device_id=&cursor=
GET /v1/conversations/{session_id}
GET /v1/conversations/{session_id}/summary
GET /v1/memories?status=candidate|confirmed
GET /v1/reviews/daily?date=2026-08-22
```

**会话数据结构**:
```json
{
  "session_id": "sess_xxx",
  "device_id": "dev_xxx",
  "subject_id": "subj_xxx",
  "started_at": "2026-08-22T10:00:00Z",
  "ended_at": "2026-08-22T10:05:00Z",
  "turns": [
    {
      "turn_id": 1,
      "user_text": "今天天气怎么样",
      "assistant_text_generated": "今天是晴天，温度25度...",
      "assistant_text_heard": "今天是晴天，温度25度...",
      "playback_status": "completed",
      "actual_heard": true
    }
  ],
  "summary": "用户询问天气情况"
}
```

**验收标准**（整改方案 5.1.F）:
- ✅ 用户实际听到的内容与"生成但未播放"分开
- ✅ 缺少精确 DAC 证据的记录标为 approximate
- ✅ 记忆候选/确认分开，确认列表仅消费 confirmed claim

---

## P2-6: 诊断功能

**目标**: 提供设备和会话的诊断信息，便于问题排查

**关键功能**:
- 最近连接时间
- 固件、Board、Runtime Profile 版本
- 配置模式与实际生效模式
- AEC 验收状态、参考类型、档案版本
- 上行丢包、下行欠载、播放 ACK、重连次数
- 一键生成脱敏诊断包

**小程序页面**: `apps/miniprogram/pages/diagnostics/index.wxml`

**API 端点**:
```
GET /v1/devices/{device_id}/diagnostics/latest
POST /v1/devices/{device_id}/diagnostics/export
```

**诊断数据结构**:
```json
{
  "device_id": "dev_xxx",
  "collected_at": "2026-08-22T12:00:00Z",
  "firmware": {
    "version": "1.0.0",
    "build_date": "2026-08-20",
    "board_profile": "atk-dnesp32s3-v1"
  },
  "runtime_profile": {
    "version": 27,
    "audio_mode": "interrupt_assist",
    "aec_verified": false
  },
  "network": {
    "wifi_rssi": -45,
    "uplink_packet_loss": 0.002,
    "downlink_underrun": 0.001,
    "reconnect_count": 2
  },
  "acoustic": {
    "aec_profile_version": "aec-v1",
    "aec_verified": false,
    "reference_type": "internal_reference"
  },
  "media_stats": {
    "playback_ack_rate": 0.998,
    "average_latency_ms": 450
  }
}
```

**验收标准**（整改方案 5.1.G）:
- ✅ 展示服务端权威版本信息
- ✅ 展示配置模式与实际生效模式，未连接时不伪造
- ✅ 展示 AEC 验收状态和档案版本
- ✅ 上行丢包、下行欠载等统计信息
- ✅ 一键导出脱敏诊断包

---

## P2-7: 小程序 PR-19 发布

**目标**: 整合所有 P2 功能，发布新版本小程序

**发布清单**:

### 代码审查
- [ ] 所有 P2-1 至 P2-6 功能已实现
- [ ] 单元测试覆盖率 > 80%
- [ ] 无 ESLint/TypeScript 错误
- [ ] 无已知安全漏洞

### 功能测试
- [ ] 配网流程在 iOS/Android 真机测试通过
- [ ] Soul/Persona 配置生效验证
- [ ] 家庭成员管理功能测试
- [ ] 设备管理功能测试
- [ ] 回顾与总结功能测试
- [ ] 诊断功能测试

### 性能测试
- [ ] 小程序包大小 < 2 MB
- [ ] 首屏加载时间 < 2 秒
- [ ] 无内存泄漏

### 合规审查
- [ ] 微信后台权限列表无麦克风权限
- [ ] 不连接 MiniProgramMediaGateway 和 LiveKit
- [ ] 不创建 RecorderManager
- [ ] 隐私政策更新
- [ ] 用户协议更新

### 发布流程
1. 提交代码到 `release/pr-19` 分支
2. 运行完整 CI/CD pipeline
3. 提交微信审核
4. 审核通过后灰度发布（10% → 50% → 100%）
5. 监控错误率和崩溃率

**验收标准**（整改方案 5.2）:
```text
✅ 微信后台权限列表中无麦克风必选权限
✅ 冷启动、首页、回顾、设备页不会创建 RecorderManager
✅ 不连接 MiniProgramMediaGateway 和 LiveKit
✅ 小程序关闭后，机器人持续完成对话
✅ 手机切换网络后，不中断机器人媒体会话
✅ 设置变更通过版本化 Runtime Profile 在设备下一安全点生效
✅ 小程序只显示权威持久化记录，不伪装成实时音频状态机
```

---

## P2 整体验收标准

**产品层面**:
- ✅ 用户可以完成设备配网并启用
- ✅ 用户可以配置机器人人格
- ✅ 用户可以管理家庭成员
- ✅ 用户可以查看对话历史和摘要
- ✅ 用户可以诊断设备问题

**技术层面**:
- ✅ 小程序定位为纯控制面（无实时音频）
- ✅ 设备独立对话（无需小程序在线）
- ✅ 所有设置通过版本化 API 更新
- ✅ 小程序只展示服务端权威状态

**安全层面**:
- ✅ 配网过程 PoP 认证
- ✅ Wi-Fi 密码加密传输
- ✅ 敏感数据不在小程序持久化
- ✅ 儿童监护限制生效

---

## 参考

- 整改方案第 5 节：小程序整改清单
- 整改方案第 6 节：设备启用与独立运行流程
- 整改方案第 10 节：服务端 API 与数据合同
