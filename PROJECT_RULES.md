# Memoria Working Agreements

本文件只保存长期有效的工程与产品规则。当前运行状态写入 `HANDOFF.md`；产品、架构和开发入口写入 `README.md`；外部研究扫描写入 `RESEARCH.md`。

## 工作方式

- 先检查 `git status`。保留用户已有改动，只暂存本任务范围；禁止 broad reset、blanket commit 和覆盖未确认的脏文件。
- 主 agent 负责拆解、风险判断、审查和验收。边界独立的搜索、机械修改和耗时测试可以委派；生产变更、删除和发布由主 agent 亲自复核。
- 变更请求默认做到实现、接线、门禁和可安全执行的发布/验收，不停在建议或本地绿色。只读审查不得擅自修改。
- 编辑后独立读回，运行 `git diff --check`，按风险执行定向测试和完整门禁。外部命令成功不等于文件内容或运行态正确。
- 涉及数据库 schema、约束、RLS、授权或跨域清单的改动，交付前必须带 `MEMORIA_TEST_POSTGRES_DSN` 至少跑一遍受影响域的 PostgreSQL 契约测试。这类用例整档被 DSN 门控，本地与沙箱默认静默跳过，因此「本地全绿」在 schema 类改动上不作为签收依据；CI `python` job 已内置 postgres service 并带着该变量跑全量，它的红才是真红。本机可由 `scripts/tests/run_authoritative_postgres_gate.sh` 的范式起一个临时 PG 复用。按类别排查同类清单断言与夹具（RLS 表集合、授权集合、幂等白名单、迁移清单、样本夹具），不要只修报出来的那一条。
- 生产事实必须查真实有效配置、挂载、容器环境、日志、数据库和设备证据；不要从默认值、目录名或 HTTP 200 推断成功。

## 文档纪律

仓库长期文档严格只有 `README.md`、`PROJECT_RULES.md`、`HANDOFF.md`、`RESEARCH.md` 四份。`RESEARCH.md` 是唯一额外文件，供外部研究助手写入扫描、开发评估并标记状态；同一文件持续合并更新。仍禁止新增 session 记录、平行计划、组件 README、ADR、release note 或一次性排障文档。

- 规则与稳定边界归入 `PROJECT_RULES.md`。
- 产品、架构、开发和协议入口归入 `README.md`。
- 当前状态、当前/回滚版本、运维步骤和下一验收归入 `HANDOFF.md`。
- 外部研究扫描、待评估项与开发状态标记只写入 `RESEARCH.md`；同一想法复现时合并更新，不另建文档。
- 机器事实优先放 schema、proto、JSON、TOML、锁文件和测试，不用 prose 重复。
- 过时内容确认无代码/运维引用后直接删除，不保留兼容文档或“归档”目录。
- 临时证据写入被忽略的 `outputs/` 或服务器证据目录，不提交到 Git。

CI 的 `tests/test_documentation_budget.py` 必须保持绿色。

## 实现原则

- 删除优先于兼容：废弃代码和路径确认无引用后直接删除，不增加 migration、fallback 或平行实现。
- 选择满足当前需求的最简单实现，不做预防性抽象或多余配置层。
- 多层系统先打通最小端到端路径，再纵向扩展；不要拆掉已经可运行的权威链。
- 组件单一职责、边界清晰。新增依赖前先盘点现有库，优先成熟且维护中的实现。
- 架构按长期方向决策；采用成熟模式，不以“先这样以后再换”制造返工。

## 产品定位

- 定位以用户 2026-09-12 重申为准：陪伴与关怀是内核，人群分阶段扩展——前期学生（陪伴、关怀、教学导师）→ 后期老年人（陪伴、关怀、人物复刻、人生故事搜集并制作成人生故事册）→ 再往后年轻人（陪伴、潮玩）。
- 机器人外形未定；吉祥物阵容（星澜、桃喜、绵绵、阿序、玄墨等）为占位资产，对外材料可暂用，不绑定最终外形。
- 声纹门禁、按需档案、带标识导出、可证明删除是能力底座，不作为定位叙事。任何「适老陪伴 / 潮玩是反定位」的旧表述作废；它们只是当前阶段不投入，不是永久不做。
- 工程与合规红线不受定位变化影响：不宣传全双工、不放宽 `reject_non_owner_voice`、声纹单独同意、未成年人走监护同意与能力门控。

## Bug 必须举一反三

修复线上异常或体验问题时，不能只修表面症状。同一轮至少检查：

1. 同文件、同状态机、同 fence/gate 是否有对称分支会再次碰撞。
2. 成功之外的失败、超时、空音频、误识别和迟到事件。
3. 级联、Omni、H5、ESP32 Direct 与 legacy 回滚链的同类路径。
4. 问题应由阈值解决，还是需要规则表、状态机或单一决策点。
5. 日志、磁盘、发布、回滚和测试门禁是否存在同类隐患。

有架构级改进时先向用户说明方案与取舍，再实施。连续出现 enroll、打断、停一下、继续、噪声或纯控制词进入 chat，通常说明话轮意图缺少统一入口，不应继续在各处堆 if/else。

## 语音控制面

`services/agent/src/orchestration/utterance_router.py` 是用户话轮意图的单一入口；`DuplexRuntime.accept_user_turn` 与 `on_real_interrupt` 共用 `route_utterance`，分类为 enroll、pure interrupt、interrupt+chat 或 chat。修打断/门禁问题时优先修改 Router 规则与 `services/agent/tests/unit/test_utterance_router.py`，不得在 runtime 新建平行控制面。

播放期歧义 final 的小模型只能给 `CONTROL_ONLY / HAS_USER_CONTENT / UNSURE` 证据：确定性规则优先，模型不得直接执行 stop/chat/ack/clear。请求只能绑定当前 speech epoch、sticky 首文本、冻结助手文本与完整 generation fence；超时、非法输出和迟到结果 fail closed。

播放期 noise、backchannel 和 echo 由 `PlaybackInputGuard` 处理。VoCat 播放期保持采集、关闭本地 KWS 停播。说话中 BOOT / 屏幕触摸仍是本地硬停；待机点屏幕不得 `ToggleChatState` 开麦，也不得因点屏震动改表情。BMI270 只把短拍脉冲当成本地惊讶脸；持续摇晃和点屏 rumble 忽略，不得开会话。任何 stop、cancel、PCM 或 playback 回执都必须带 `session_epoch + turn_id + generation_id + tool_epoch`，旧代先于连续性检查丢弃。

## 身份、历史与权限

- `reject_non_owner_voice` 默认开启，只拒绝 formal `guest/owner_mismatch` 和明确 `shadow_guest_candidate`。
- shadow/formal ambiguous 可以普通对话，但保持 non-owner/uncertain，不能进入主人历史、私人记忆、工具或敏感权限。关闭过滤也不能把访客升级为 owner。
- H5 长期历史只消费 Agent 权威终稿 `history_eligible=true`。资格按原话轮 generation fence 绑定；不得读取“当前最新说话人”替代归属。
- 学生线账号能力以 `services/control_api/app/account_gate.py` 为唯一决策表。每个端点在私有读取或副作用前调用 `require_capability_for_subject`；代操作使用 `require_capability_for_account_id`。adult、minor、类别缺失都要有矩阵测试；未声明能力拒绝。
- 账号登录、说话人判定、目标说话人聚焦和敏感动作授权是四个不同结论，不得互相升级。

## 客户端与视觉

- H5 做实质视觉修改前，先以当前运行页面、明确设计源或用户选定 mock 为权威；自己启动预览并做真实移动视口检查。
- 吉祥物采用 3D 位图机身、内联 SVG 表情和克制 CSS 动效；不为简单表情引入 Rive、Live2D 或路径 morph，完整尊重 `prefers-reduced-motion`。
- 用户情绪只消费权威 `emotion_observation`；助手说话表情只消费当前 speaking fence 的 `assistant_expression`，回答结束、断线或中断立即清除。客户端禁止从字幕猜词切换表情。
- 新用户先选星澜、桃喜、绵绵、阿序或玄墨（占位阵容，不绑定最终外形，见「产品定位」）；选角支持 scroll-snap、可见箭头、分页点和键盘操作，并展示性格、四种表情和设计音色试听。
- 声纹登记按自然、轻声、带笑、认真顺序解锁，各段独立参与匹配。授权必须明确、可撤销；shadow 档案不得宣传为主人认证或声音克隆。
- 称呼只在注册 UI 设置，文案“怎么称呼你？”；H5“我的”和小程序个人信息不再暴露称呼或陪伴方式编辑。
- 供应商 `voice_id` 只能由 Agent 批准 registry 解析；客户端只传稳定目录键，试听文件路径包含供应商和版本。
- 微信小程序是控制面：不申请 `scope.record`，不创建 RecorderManager，不播放实时 TTS，不建立媒体 WSS，不加入 LiveKit。
- 设备屏幕表情是「对话脸」：360 圆屏黑底白描，签名是嘴（待命短平线），鼻子是米粒点，闭眼仍是月牙、睁眼是杏仁白眼加挖空瞳孔。几何以固件渲染源 `firmware/esp32/overlay/files/main/boards/memoria/esp-vocat/memoria_face.cc` 为准，预览脚本与宿主测试编译同一份源码；未知情绪回落 `neutral`，不得回退到彩色 emoji，面部不绑定唤醒名。

## 固件与硬件安全

- 固件只维护固定 upstream + overlay。overlay 变化后从锁定 commit 重放并执行 clean build、patch/依赖锁门禁。
- `0x10000..0x1ffff` 身份区不得被普通固件更新覆盖。刷写前后都回读并逐字节比较；优先 app-only `0x20000`。
- “编译通过”“刷写成功”“启动/激活”“真实媒体”“Actual Heard”“打断/双讲”是独立证据层，禁止相互外推。
- 真实设备验收必须绑定候选 commit、固件摘要、板卡身份摘要、session/stream/generation fence 和用户听感确认。
- 没有 Exact DAC/AEC Reference/Double-talk/T1–T14 证据时，`direct_real_device_verified` 与 `full_duplex_verified` 保持 false，产品不得宣传全双工。

## 当前出货声学契约

- 当前硬件 SKU 是 ESP-VoCat（ES7210 双麦 + ES8311，`board_profile=memoria-esp-vocat`）。旧 ATK ES8388 单麦半双工板已退役，不得再作为实现约束或对客口径。
- 协商上限默认 `audio_mode=interrupt_assist`：hello 如实报 simultaneous capture 与 AEC reference，`aec_reference_verified=false`。Agent 只按协商 `audio_mode` 开 barge-in，禁止「凡 device_session 都半双工」。
- `full_duplex_verified` 另需 Edge 声学 registry 登记、真实 AEC residual、双讲 T1–T14 和 Actual Heard。未过证不得改 hello `aec_reference_verified`，不得宣传全双工。
- 播放期保持采集；说话中 BOOT / 屏幕触摸仍是本地硬停。待机点屏幕和拍身体不得开麦。禁止用云端 holdoff、丢弃 VAD 边沿或加大 DTLN 增益去假装 AEC。
- `TurnPhase` 在 interrupt_assist 真机打断未 `verified` 前，不得从 shadow 改为有副作用的生产策略。
- 路演、对客口径必须与 `HANDOFF.md` 的 `advertised_duplex_level` 一致。当前工单步骤只写在 `HANDOFF.md`，不另建计划文档。

## 发布、回滚与保留

- 发布按组件最小切片，先冻结 source/image/manifest 摘要和回滚点，再切流、冒烟、延迟复核。
- `code / wired / enabled / verified` 必须分开记录，并附证据日期。零会话 readiness、首帧或容器 healthy 不是真实设备完整会话。
- 生产普通制品只保留当前运行版本和一个已验证可运行的紧邻回滚。新版本与回滚点核验后删除更早上传包、构建归档、候选/回滚镜像并检查磁盘。
- 数据库、WAL、MinIO、安全和合规备份不属于普通制品，按独立策略保留，禁止误删。
- 数据恢复必须从不可变证据重建投影；不得把缓存、派生索引或同机备份描述为异地灾备。
- 生产环境、密钥、WMS/Nginx 既有路由和非目标容器不因局部发布而改变。任何超出用户授权的生产操作先停下确认。

## 验收用语

交付结论要明确区分：

- `code`：实现存在且静态/单测通过。
- `wired`：真实主链调用了实现。
- `enabled`：候选运行配置已开启。
- `verified`：指定环境和场景有可复核证据。

Actual Heard 只能由设备播放终端证据和用户听感共同确认。若证据不足，直接写 pending/unknown，不从旧 release 或其他板卡继承。
