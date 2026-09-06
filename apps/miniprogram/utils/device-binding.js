/*
 * 设备绑定与主体切换的客户端领域逻辑（对应整改文档 §2 / §4 / §9 / §10）。
 *
 * 边界约定：
 * - 客户端只构建 §9.1 POST /v1/device-bindings 合同允许的字段；出现任何
 *   policy_version / *_accepted / authorized 之类的“假授权”字段一律抛错，
 *   由 buildBindingRequest 在发送前 fail-closed。
 * - 首次绑定的“最小必要信息”按场景采集；年龄带只用于默认体验文案与展示，
 *   不写入绑定请求（正式年龄证据由服务端验证，见 PR-02）。
 * - 数字自我 / 声音复刻 / 传承等敏感授权默认不勾选；“父母本人接受”类
 *   consent 只能由父母本人确认，客户端不得代为提交。
 * - Runtime Profile（§9.3）与服务端主体解析（§9.2）的响应统一在此做
 *   fail-closed 规范化：未知 service_mode 一律落到 unknown_safe，未知
 *   speaker_state 一律 unknown，缺失 capabilities 一律空集。
 */

const contracts = require("./multi-subject-contracts");

const MODE_META = Object.freeze({
  parent_for_child: {
    title: "给孩子使用",
    tagline: "学习、陪伴与成长记录",
    description:
      "将建立儿童主体与监护关系；学习、时长与监护规则由服务端统一管控，人格不会覆盖儿童策略。",
  },
  self_use: {
    title: "给自己使用",
    tagline: "专属你的私人陪伴",
    description:
      "建立本人主体；记忆、声音、数字自我等能力分别授权，敏感能力默认关闭。",
  },
  child_for_parent: {
    title: "给父母使用",
    tagline: "管理归你，内容归父母本人",
    description:
      "区分设备管理员与实际使用者；子女可协助管理设备，但不能默认读取父母私人内容。",
  },
  family_shared: {
    title: "家庭共同使用",
    tagline: "全家共用，每人独立档案",
    description:
      "建立家庭空间与成员独立档案；私人记忆、家庭共享记忆与待确认记忆分区管理，无法确认说话人时安全降级。",
  },
});

/*
 * 主体与账号持有人的关系（primary_subject.relationship）。
 * 与后端 PR-04/PR-05 合同对齐，客户端不自行发明取值。
 */
const PRIMARY_RELATIONSHIPS = Object.freeze({
  parent_for_child: "guardian_of",
  self_use: "self",
  child_for_parent: "child_of",
  family_shared: "family_member_of",
});

/*
 * ConsentOffer 目录（PR-04 服务端必须验证 offer id；客户端只提交给用户
 * 展示过且可代为确认的 offer）。requiresParentSelfAcceptance 的 offer
 * 只能由父母本人确认，客户端不得写入 consent_offer_ids。
 */
const CONSENT_OFFERS = Object.freeze([
  {
    id: "offer_minor_voice_session_v1",
    modes: ["parent_for_child"],
    label: "语音陪伴与学习会话",
    description: "孩子可以进入陪伴和导师语音会话",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_minor_memory_retention_v1",
    modes: ["parent_for_child"],
    label: "学习与成长记录",
    description: "保存脱敏转写与学习进度；不保存原始音频",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_guardian_weekly_summary_v1",
    modes: ["parent_for_child"],
    label: "每周成长小结",
    description: "只分享趋势、学习时长和话题分布，不含对话原文",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_emergency_contact_v1",
    modes: ["parent_for_child"],
    label: "紧急联系人通知",
    description: "仅在风险场景下通知你设定的紧急联系人",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_memory_retention_v1",
    modes: ["self_use"],
    label: "长期记忆",
    description: "让伙伴记住你们聊过的事，形成你自己的回忆",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_voice_profile_v1",
    modes: ["self_use"],
    label: "声纹档案",
    description: "用于识别是你本人在说话，而不是别人",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_raw_audio_v1",
    modes: ["self_use"],
    label: "原始音频留存",
    description: "单独授权保存你的原始语音",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_voice_clone_v1",
    modes: ["self_use"],
    label: "声音复刻",
    description: "生成你的声音模型；默认不开启",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_digital_self_v1",
    modes: ["self_use"],
    label: "数字自我预览",
    description: "在 AI 陪伴中使用你的数字自我；默认不开启",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_self_legacy_v1",
    modes: ["self_use"],
    label: "未来传承",
    description: "为未来授权访问做准备；默认不开启",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_senior_service_acceptance_v1",
    modes: ["child_for_parent"],
    label: "服务与数据规则（父母本人确认）",
    description: "需要父母本人在设备上确认接受后才能开启",
    defaultChecked: false,
    requiresParentSelfAcceptance: true,
  },
  {
    id: "offer_senior_memory_retention_v1",
    modes: ["child_for_parent"],
    label: "父母本人的人生记录",
    description: "父母本人确认后才会开启",
    defaultChecked: false,
    requiresParentSelfAcceptance: true,
  },
  {
    id: "offer_admin_device_management_v1",
    modes: ["child_for_parent"],
    label: "子女设备管理",
    description: "管理网络、固件与绑定；不含聊天内容读取",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_senior_health_reminder_v1",
    modes: ["child_for_parent"],
    label: "健康提醒",
    description: "仅作提醒，不作诊断，不替代专业建议",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_senior_anti_fraud_v1",
    modes: ["child_for_parent"],
    label: "防诈骗提醒",
    description: "转账、冒充家人等场景给出提醒",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_senior_emergency_contact_v1",
    modes: ["child_for_parent"],
    label: "紧急联系人与联系家人",
    description: "需要时联系父母本人设定的紧急联系人",
    defaultChecked: false,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_family_space_v1",
    modes: ["family_shared"],
    label: "家庭空间与成员独立档案",
    description: "每位成员拥有独立主体档案",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_family_shared_memory_v1",
    modes: ["family_shared"],
    label: "家庭共享记忆",
    description: "共享内容需相关成员确认后可见",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
  {
    id: "offer_family_admin_v1",
    modes: ["family_shared"],
    label: "家庭管理员职责",
    description: "管理成员与设备；不包含查看成员私人记忆",
    defaultChecked: true,
    requiresParentSelfAcceptance: false,
  },
]);

const OFFER_BY_ID = Object.freeze(
  CONSENT_OFFERS.reduce((map, offer) => {
    map[offer.id] = offer;
    return map;
  }, {}),
);

/*
 * service_preferences 白名单（按模式）。未知键或非法值一律抛错，
 * 防止通过偏好字段夹带策略/授权信息。
 */
const MODE_PREFERENCE_SCHEMAS = Object.freeze({
  parent_for_child: {
    tutor_enabled: "boolean",
    english_practice_enabled: "boolean",
    memory_level: { enum: ["none", "growth_summary"], default: "growth_summary" },
    max_session_minutes: "minutes",
    quiet_hours: "quiet_hours",
  },
  self_use: {
    memory_level: { enum: ["none", "personal"], default: "personal" },
    interview_frequency: { enum: ["off", "low", "medium"], default: "low" },
  },
  child_for_parent: {
    speech_speed: { enum: ["normal", "slow"], default: "slow" },
    memory_level: { enum: ["none", "personal"], default: "none" },
    admin_visibility: {
      enum: ["device_status", "device_status_and_reminders"],
      default: "device_status",
    },
  },
  family_shared: {
    memory_level: { enum: ["family_shared"], default: "family_shared" },
    shared_persona_enabled: "boolean",
  },
});

/* 各模式的年龄带展示选项（仅 UI 展示与默认文案，不写入绑定请求）。 */
const MODE_AGE_BANDS = Object.freeze({
  parent_for_child: ["under_14", "14_17"],
  child_for_parent: ["adult"],
  self_use: ["adult"],
  family_shared: [],
});

const AGE_BAND_LABELS = Object.freeze({
  unknown: "未确认",
  under_14: "14 岁以下",
  "14_17": "14 至 17 岁",
  adult: "18 岁及以上",
});

/* 敏感入口：是否展示由服务端 Runtime Profile 的 capabilities 决定（D-07）。
 * 可导航入口全部 fail-closed：Profile 缺失/非法/过期时一律不开放，
 * 客户端不得用本地年龄或账号资料做成人降级。手机声纹录取已移除（整改方案
 * PR-02），voice_profile_create 只在「我的」页作为服务端授权状态展示，
 * 不再映射到任何页面入口；实际说话人登记在机器人端完成。 */
const SENSITIVE_ENTRIES = Object.freeze([
  {
    key: "digital_self",
    capability: contracts.Capability.DigitalSelfPreview,
    title: "数字分身成长",
    description: "查看服务端的成长维度、Persona 与版本状态",
    page: "/pages/digital-self/index",
  },
  {
    key: "guardian_summary",
    capability: contracts.Capability.GuardianSummaryView,
    title: "成长小结与监护授权",
    description: "查看已绑定孩子的每周聚合小结",
    page: "/pages/guardian/index",
  },
  {
    key: "raw_voice_consent",
    capability: contracts.Capability.RawAudioRetention,
    title: "原始语音授权",
    description: "查看或撤回原始音频归档授权",
    page: "/pages/privacy/index",
  },
  {
    key: "memory_recall",
    capability: contracts.Capability.MemoryRecallPrivate,
    title: "私人回顾",
    description: "查看你的私人记忆",
    page: "/pages/memory/index",
  },
]);

/* 客户端消费的五类敏感能力（§9.4 / D-07）。其余敏感能力（支付、复刻、
 * 传承、训练等）小程序侧没有入口，一律不消费。 */
const SENSITIVE_CAPABILITIES = Object.freeze([
  contracts.Capability.VoiceProfileCreate,
  contracts.Capability.DigitalSelfPreview,
  contracts.Capability.GuardianSummaryView,
  contracts.Capability.RawAudioRetention,
  contracts.Capability.MemoryRecallPrivate,
]);

function entryForCapability(capability) {
  return SENSITIVE_ENTRIES.find((entry) => entry.capability === capability) || null;
}

/*
 * 配置/consent 类动作的独立 gate seam（P1 边界）：监护关系创建与授权、
  * 原始语音授权授予/撤回等“配置能力”的动作不能复用使用类
 * 能力的门禁（否则首次设置死锁：未授权就永远无法发起授权），也不能在
 * 客户端用年龄绕过。后端 consent/policy 决策接口接入前，一律 fail-closed
 * 并给出可解释文案；接入后由该 seam 消费服务端决策结果。
 */
const CONFIG_SEAM_MESSAGES = Object.freeze({
  guardian_manage:
    "监护关系与授权配置需要服务端 consent/policy 决策接口；该接口尚未接入，操作保持关闭，不会按本地年龄放开。",
  raw_audio_consent:
    "原始语音授权配置需要服务端 consent 决策接口；该接口尚未接入，授权保持关闭，不会按本地年龄放开。",
});

function configActionGate(action) {
  return {
    allowed: false,
    reason: "config_seam_pending",
    message:
      CONFIG_SEAM_MESSAGES[action] || "该配置动作的服务端决策接口尚未接入，保持关闭。",
  };
}

/*
 * 敏感入口被拒绝时的可解释文案。reason 来自 requireRuntimeCapability
 * （no_binding / invalid_profile / capability_missing / superseded / unavailable）。
 */
function capabilityGateMessage(result, capability) {
  const title = entryForCapability(capability)?.title || "该能力";
  if (result?.reason === "unauthenticated") {
    return "请先登录，再查看这项内容。";
  }
  if (result?.reason === "binding_sync_failed") {
    return "设备信息尚未同步，暂时无法查看这项内容。请到「设备」页重新同步，无需重新绑定。";
  }
  if (result?.reason === "device_selection_required") {
    return "账号下有多台设备，请先到「设备」页选择当前设备。";
  }
  if (result?.reason === "no_binding") {
    return "还没有绑定设备，无法取得 Runtime Profile；请先在「设备与成员」完成首次绑定。";
  }
  if (result?.reason === "invalid_profile" || result?.reason === "unavailable") {
    return "服务端暂时无法提供有效的 Runtime Profile，此功能保持关闭。";
  }
  if (result?.reason === "superseded") {
    return "能力校验未通过，入口保持关闭，请刷新后重试。";
  }
  return `${title}尚未开放：服务端未按当前主体授权。`;
}

const REQUEST_ALLOWED_KEYS = Object.freeze([
  "claim_id",
  "onboarding_session_id",
  "declared_mode",
  "account_owner_person_id",
  "primary_subject",
  "persona_selection",
  "service_preferences",
  "consent_offer_ids",
]);

const SUBJECT_ALLOWED_KEYS = Object.freeze(["person_id", "relationship", "subject_draft"]);
const SUBJECT_DRAFT_ALLOWED_KEYS = Object.freeze(["display_name", "age_band"]);

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function assertNonEmptyString(value, field) {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${field} 不能为空`);
  }
}

function validateQuietHours(value) {
  if (value === null || value === undefined) return null;
  if (!isPlainObject(value)) throw new TypeError("quiet_hours 格式无效");
  const { start, end } = value;
  if (
    typeof start !== "string" ||
    typeof end !== "string" ||
    !/^\d{2}:\d{2}$/.test(start) ||
    !/^\d{2}:\d{2}$/.test(end)
  ) {
    throw new TypeError("quiet_hours 需要 HH:MM 格式的 start 与 end");
  }
  return { start, end };
}

function validatePreferenceValue(schema, value, key) {
  if (schema === "boolean") {
    if (typeof value !== "boolean") throw new TypeError(`${key} 必须是布尔值`);
    return value;
  }
  if (schema === "minutes") {
    const isValidMinutes =
      typeof value === "number" &&
      Number.isFinite(value) &&
      value >= 5 &&
      value <= 120 &&
      Math.floor(value) === value;
    if (!isValidMinutes) {
      throw new TypeError(`${key} 必须是 5 到 120 的整数分钟`);
    }
    return value;
  }
  if (schema === "quiet_hours") return validateQuietHours(value);
  if (isPlainObject(schema) && Array.isArray(schema.enum)) {
    if (!schema.enum.includes(value)) {
      throw new TypeError(`${key} 取值无效：${value}`);
    }
    return value;
  }
  throw new TypeError(`${key} 缺少校验规则`);
}

function validateServicePreferences(mode, preferences) {
  const schema = MODE_PREFERENCE_SCHEMAS[mode];
  if (preferences === undefined || preferences === null) return {};
  if (!isPlainObject(preferences)) throw new TypeError("service_preferences 格式无效");
  const normalized = {};
  for (const [key, value] of Object.entries(preferences)) {
    if (!Object.prototype.hasOwnProperty.call(schema, key)) {
      throw new TypeError(`service_preferences 不允许字段 ${key}`);
    }
    normalized[key] = validatePreferenceValue(schema[key], value, key);
  }
  return normalized;
}

function validateConsentOffers(mode, offerIds) {
  if (offerIds === undefined || offerIds === null) return [];
  if (!Array.isArray(offerIds)) throw new TypeError("consent_offer_ids 必须是数组");
  const unique = new Set();
  for (const offerId of offerIds) {
    if (typeof offerId !== "string" || !offerId) {
      throw new TypeError("consent_offer_ids 包含非法项");
    }
    const offer = OFFER_BY_ID[offerId];
    if (!offer) throw new TypeError(`未知的 consent offer：${offerId}`);
    if (!offer.modes.includes(mode)) {
      throw new TypeError(`consent offer ${offerId} 不适用于 ${mode}`);
    }
    if (offer.requiresParentSelfAcceptance) {
      throw new TypeError(
        `consent offer ${offerId} 需要父母本人确认，客户端不能代为提交`,
      );
    }
    unique.add(offerId);
  }
  return [...unique];
}

function validatePrimarySubject(mode, subject) {
  if (!isPlainObject(subject)) throw new TypeError("primary_subject 格式无效");
  for (const key of Object.keys(subject)) {
    if (!SUBJECT_ALLOWED_KEYS.includes(key)) {
      throw new TypeError(`primary_subject 不允许字段 ${key}`);
    }
  }
  assertNonEmptyString(subject.person_id, "primary_subject.person_id");
  const expectedRelationship = PRIMARY_RELATIONSHIPS[mode];
  if (subject.relationship !== expectedRelationship) {
    throw new TypeError(`primary_subject.relationship 必须是 ${expectedRelationship}`);
  }
  const normalized = { person_id: subject.person_id, relationship: subject.relationship };
  if (subject.subject_draft !== undefined) {
    if (subject.person_id !== "new") {
      throw new TypeError("只有 person_id=new 时才能携带 subject_draft");
    }
    if (!isPlainObject(subject.subject_draft)) {
      throw new TypeError("subject_draft 格式无效");
    }
    for (const key of Object.keys(subject.subject_draft)) {
      if (!SUBJECT_DRAFT_ALLOWED_KEYS.includes(key)) {
        throw new TypeError(`subject_draft 不允许字段 ${key}`);
      }
    }
    assertNonEmptyString(subject.subject_draft.display_name, "subject_draft.display_name");
    const ageBand = subject.subject_draft.age_band;
    if (!contracts.isAgeBand(ageBand)) {
      throw new TypeError(`subject_draft.age_band 取值无效：${ageBand}`);
    }
    const allowedBands = MODE_AGE_BANDS[mode];
    if (allowedBands.length > 0 && !allowedBands.includes(ageBand)) {
      throw new TypeError(`age_band ${ageBand} 不适用于 ${mode}`);
    }
    normalized.subject_draft = {
      display_name: subject.subject_draft.display_name.trim(),
      age_band: ageBand,
    };
  }
  return normalized;
}

/*
 * 构建 POST /v1/device-bindings 请求体（§9.1）。
 * 只接受白名单字段；任何 policy_version / *_accepted / authorized 字段
 * 或未知键都会抛错（fail-closed），保证客户端不会提交“假授权”。
 */
function buildBindingRequest(request) {
  if (!isPlainObject(request)) throw new TypeError("绑定请求格式无效");
  for (const key of Object.keys(request)) {
    if (!REQUEST_ALLOWED_KEYS.includes(key)) {
      if (/accepted|authorized|policy_version|consent_granted/i.test(key)) {
        throw new TypeError(`禁止客户端提交授权声明字段 ${key}`);
      }
      throw new TypeError(`绑定请求不允许字段 ${key}`);
    }
  }
  assertNonEmptyString(request.claim_id, "claim_id");
  assertNonEmptyString(request.onboarding_session_id, "onboarding_session_id");
  if (!contracts.isDeviceDeclaredMode(request.declared_mode)) {
    throw new TypeError(`declared_mode 取值无效：${request.declared_mode}`);
  }
  assertNonEmptyString(request.account_owner_person_id, "account_owner_person_id");
  assertNonEmptyString(request.persona_selection, "persona_selection");
  if (request.persona_selection.length > 64) {
    throw new TypeError("persona_selection 过长");
  }
  return {
    claim_id: request.claim_id.trim(),
    onboarding_session_id: request.onboarding_session_id.trim(),
    declared_mode: request.declared_mode,
    account_owner_person_id: request.account_owner_person_id.trim(),
    primary_subject: validatePrimarySubject(request.declared_mode, request.primary_subject),
    persona_selection: request.persona_selection.trim(),
    service_preferences: validateServicePreferences(
      request.declared_mode,
      request.service_preferences,
    ),
    consent_offer_ids: validateConsentOffers(
      request.declared_mode,
      request.consent_offer_ids,
    ),
  };
}

function consentOffersFor(mode) {
  return CONSENT_OFFERS.filter((offer) => offer.modes.includes(mode)).map((offer) => ({
    id: offer.id,
    label: offer.label,
    description: offer.description,
    defaultChecked: offer.defaultChecked,
    requiresParentSelfAcceptance: offer.requiresParentSelfAcceptance,
  }));
}

function stringOrNull(value) {
  return typeof value === "string" && value ? value : null;
}

function numberOrNull(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/*
 * Runtime Profile（§9.3 / packages/contracts RuntimeProfileSigned）严格
 * fail-closed 规范化。客户端不持有 HMAC 密钥，只做 TLS 响应边界 + 信封结构
 * 校验（signature 必须是 hex64 形状，不验证 HMAC 值）。以下任一情况整体
 * fail closed 为 unknown_safe + 空 capabilities（空敏感能力）：
 *   - 缺失任何必需字段、出现任何额外字段、字段类型/枚举值非法；
 *   - string/null/0 的 binding_version、0 或倒退的 session_epoch；
 *   - 过期（expires_at <= now / <= issued_at）、issued_at 晚于当前时间；
 *   - signature 缺失或形状错误；
 *   - 非法关系：speaker_state != confirmed 却带 active_subject_id（或
 *     confirmed 却没有）、subject_category 与 age_band 矛盾；
 *   - unconfirmed/unknown 说话人或 subject_category=unknown 却携带敏感能力。
 */
const RUNTIME_PROFILE_KEYS = Object.freeze([
  "signature_schema",
  "runtime_profile_id",
  "device_id",
  "session_id",
  "actor_id",
  "binding_id",
  "binding_version",
  "active_subject_id",
  "subject_revision",
  "subject_category",
  "age_band",
  "speaker_state",
  "speaker_confidence",
  "service_mode",
  "persona_assignment_id",
  "persona",
  "policy_bundle_version",
  "capabilities",
  "obligations",
  "policy_receipt_ids",
  "session_epoch",
  "issued_at",
  "expires_at",
  "signature",
]);
const RUNTIME_PROFILE_KEY_SET = new Set(RUNTIME_PROFILE_KEYS);
const PERSONA_KEYS = Object.freeze(["persona_id", "version", "relationship_stage"]);
const HEX64_PATTERN = /^[a-f0-9]{64}$/;

const CATEGORY_FOR_AGE_BAND = Object.freeze({
  unknown: contracts.SubjectCategory.Unknown,
  under_14: contracts.SubjectCategory.Minor,
  "14_17": contracts.SubjectCategory.Minor,
  adult: contracts.SubjectCategory.Adult,
});

/*
 * unknown_safe 模式（§4.3）只允许不落长期记忆的常规能力：
 * 普通聊天与临时英语练习；不允许 tutor（完整作业/学习模式）或任何敏感能力。
 * 基础设备控制等能力若未来进入 canonical Capability，再按同样白名单收敛。
 */
const UNKNOWN_SAFE_ALLOWED_CAPABILITIES = Object.freeze([
  contracts.Capability.Chat,
  contracts.Capability.EnglishPractice,
]);

/*
 * minor 主体结构层禁止的能力集合（§4.2 能力矩阵“儿童/学生”列）：
 * - voice_clone_use（声音复刻）：默认禁止；
 * - digital_self_preview（数字自我）：禁止或仅成人后重新授权；
 * - legacy_grant_create（人生传承）：禁止；
 * - device_ownership_transfer（所有权转移）：不属于儿童。
 * 客户端只做结构/cross-invariant 防御，不自行扩展产品权限矩阵：
 * voice_profile_create / memory_recall_private / guardian_summary_view
 * 等在文档中面向儿童明确可用（主体分流需治理 / 分项同意最小化 /
 * actor=guardian 而 subject=minor），由服务端权威 profile 签发并带义务，
 * 客户端结构层不得永久禁死；payment / raw_audio_retention /
 * model_training_contribution 属于“默认禁用或需额外确认”，若权威 profile
 * 确实签发并携带义务，同样只由使用入口依 capability 收敛，不在结构层拒绝。
 */
const MINOR_FORBIDDEN_CAPABILITIES = Object.freeze([
  contracts.Capability.VoiceCloneUse,
  contracts.Capability.DigitalSelfPreview,
  contracts.Capability.LegacyGrantCreate,
  contracts.Capability.DeviceOwnershipTransfer,
]);

/* 成人/适老服务模式：minor 主体一律不允许（权威 profile 签发除外项不适用）。 */
const MINOR_FORBIDDEN_MODES = Object.freeze([
  contracts.ServiceMode.AdultCompanion,
  contracts.ServiceMode.AdultArchive,
  contracts.ServiceMode.SelfPreview,
  contracts.ServiceMode.LegacyAccess,
  contracts.ServiceMode.SeniorCompanion,
]);

function isNonEmptyString(value, maxLength = 128) {
  // 拒绝纯空白与首尾空白，避免 ID 字符串在缓存键/比较中出现歧义。
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maxLength &&
    value.trim() === value
  );
}

/*
 * 严格 RFC3339/ISO-8601 时间校验：必须携带时区（Z 或 ±HH:MM），
 * 拒绝 Date.parse 能容忍的宽松自然语言（如 "2026-08-09"、"August 9"）。
 * 同时做日历真实性校验（拒绝 2026-02-30、非闰年 02-29 等滚动日期）、
 * 小时/分钟/秒边界（拒绝 24:00）与真实时区偏移边界（|offset| <= 14:00，
 * 对应 IANA 实际时区范围；+99:99 / +00:60 / +14:30 一律拒绝）。
 */
/* RFC3339 §5.6：T/Z 大小写均可（canonical 合同统一允许 lowercase t/z）。 */
const RFC3339_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})$/;
const MAX_UTC_OFFSET_MINUTES = 14 * 60;

function parseRfc3339(value) {
  if (typeof value !== "string") return null;
  const match = RFC3339_PATTERN.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;
  const zone = match[8];
  let offsetMinutes = 0;
  if (zone !== "Z" && zone !== "z") {
    const sign = zone[0] === "-" ? -1 : 1;
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return null;
    offsetMinutes = sign * (offsetHour * 60 + offsetMinute);
    if (Math.abs(offsetMinutes) > MAX_UTC_OFFSET_MINUTES) return null;
  }
  const utcMs = Date.UTC(year, month - 1, day, hour, minute, second) - offsetMinutes * 60_000;
  const wall = new Date(utcMs + offsetMinutes * 60_000);
  if (Number.isNaN(wall.getTime())) return null;
  // 日历真实性：wall 分量必须与输入完全一致，拒绝滚动日期。
  if (
    wall.getUTCFullYear() !== year ||
    wall.getUTCMonth() + 1 !== month ||
    wall.getUTCDate() !== day ||
    wall.getUTCHours() !== hour ||
    wall.getUTCMinutes() !== minute ||
    wall.getUTCSeconds() !== second
  ) {
    return null;
  }
  return utcMs;
}

function isIsoDateTime(value) {
  return parseRfc3339(value) !== null;
}

function isUniqueArray(value, predicate, maxItems) {
  if (!Array.isArray(value) || value.length > maxItems) return false;
  if (new Set(value).size !== value.length) return false;
  return value.every(predicate);
}

function failClosedRuntimeProfile(reasons) {
  return {
    valid: false,
    degraded: true,
    fail_reasons: reasons,
    signature_schema: "runtime-profile-v1",
    runtime_profile_id: null,
    device_id: null,
    session_id: null,
    actor_id: null,
    binding_id: null,
    binding_version: null,
    active_subject_id: null,
    subject_revision: null,
    subject_category: contracts.SUBJECT_CATEGORY_DEFAULT,
    age_band: contracts.AGE_BAND_DEFAULT,
    speaker_state: contracts.SPEAKER_STATE_DEFAULT,
    speaker_confidence: null,
    service_mode: contracts.SERVICE_MODE_DEFAULT,
    persona_assignment_id: null,
    persona: null,
    policy_bundle_version: null,
    capabilities: [],
    obligations: [],
    policy_receipt_ids: [],
    session_epoch: null,
    issued_at: null,
    expires_at: null,
    signature: null,
  };
}

function validatePersona(value) {
  if (!isPlainObject(value)) return null;
  for (const key of Object.keys(value)) {
    if (!PERSONA_KEYS.includes(key)) return null;
  }
  if (!isNonEmptyString(value.persona_id)) return null;
  if (!Number.isInteger(value.version) || value.version < 1) return null;
  if (!isNonEmptyString(value.relationship_stage, 64)) return null;
  return {
    persona_id: value.persona_id,
    version: value.version,
    relationship_stage: value.relationship_stage,
  };
}

function normalizeRuntimeProfile(payload, options = {}) {
  const now = options.now !== undefined ? options.now : Date.now();
  const reasons = [];
  if (!isPlainObject(payload)) {
    return failClosedRuntimeProfile(["runtime profile 响应不是对象"]);
  }
  for (const key of Object.keys(payload)) {
    if (!RUNTIME_PROFILE_KEY_SET.has(key)) {
      reasons.push(`存在未知字段 ${key}`);
    }
  }
  for (const key of RUNTIME_PROFILE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(payload, key)) {
      reasons.push(`缺少必需字段 ${key}`);
    }
  }
  if (reasons.length > 0) return failClosedRuntimeProfile(reasons);

  if (payload.signature_schema !== "runtime-profile-v1") {
    reasons.push("signature_schema 必须是 runtime-profile-v1");
  }
  for (const key of [
    "runtime_profile_id",
    "device_id",
    "session_id",
    "actor_id",
    "binding_id",
    "persona_assignment_id",
    "policy_bundle_version",
  ]) {
    if (!isNonEmptyString(payload[key])) reasons.push(`${key} 必须是非空字符串（≤128）`);
  }
  if (!Number.isInteger(payload.binding_version) || payload.binding_version < 1) {
    reasons.push("binding_version 必须是 >= 1 的整数（string/0/null 一律拒绝）");
  }
  const activeSubjectId = payload.active_subject_id;
  if (activeSubjectId !== null && !isNonEmptyString(activeSubjectId)) {
    reasons.push("active_subject_id 必须是字符串或 null");
  }
  if (!Number.isInteger(payload.subject_revision) || payload.subject_revision < 0) {
    reasons.push("subject_revision 必须是 >= 0 的整数");
  }
  if (!contracts.isSubjectCategory(payload.subject_category)) {
    reasons.push(`subject_category 取值无效：${payload.subject_category}`);
  }
  if (!contracts.isAgeBand(payload.age_band)) {
    reasons.push(`age_band 取值无效：${payload.age_band}`);
  }
  if (!contracts.isSpeakerState(payload.speaker_state)) {
    reasons.push(`speaker_state 取值无效：${payload.speaker_state}`);
  }
  const confidence = payload.speaker_confidence;
  if (
    confidence !== null &&
    !(typeof confidence === "number" && Number.isFinite(confidence) && confidence >= 0 && confidence <= 1)
  ) {
    reasons.push("speaker_confidence 必须是 0..1 的数字或 null");
  }
  if (!contracts.isServiceMode(payload.service_mode)) {
    reasons.push(`service_mode 取值无效：${payload.service_mode}`);
  }
  if (!isUniqueArray(payload.capabilities, contracts.isCapability, 32)) {
    reasons.push("capabilities 必须是去重且全部合法的 canonical 能力");
  }
  if (!isUniqueArray(payload.obligations, contracts.isPolicyObligation, 32)) {
    reasons.push("obligations 必须是去重且全部合法的 canonical 义务");
  }
  if (
    !isUniqueArray(
      payload.policy_receipt_ids,
      (item) => isNonEmptyString(item),
      64,
    )
  ) {
    reasons.push("policy_receipt_ids 必须是去重的非空字符串数组");
  }
  if (!Number.isInteger(payload.session_epoch) || payload.session_epoch < 1) {
    reasons.push("session_epoch 必须是 >= 1 的整数（0 禁止出现在已签发 profile 上）");
  }
  if (!isIsoDateTime(payload.issued_at)) reasons.push("issued_at 必须是 ISO 时间");
  if (!isIsoDateTime(payload.expires_at)) reasons.push("expires_at 必须是 ISO 时间");
  if (typeof payload.signature !== "string" || !HEX64_PATTERN.test(payload.signature)) {
    reasons.push("signature 必须是 64 位十六进制字符串");
  }

  const persona = validatePersona(payload.persona);
  if (!persona) reasons.push("persona 快照结构非法");

  if (reasons.length > 0) return failClosedRuntimeProfile(reasons);

  const issuedMs = parseRfc3339(payload.issued_at);
  const expiresMs = parseRfc3339(payload.expires_at);
  if (expiresMs <= issuedMs) {
    reasons.push("expires_at 必须晚于 issued_at");
  }
  if (expiresMs <= now) {
    reasons.push("runtime profile 已过期");
  }
  if (issuedMs > now) {
    reasons.push("issued_at 不能晚于当前时间");
  }

  const speakerState = payload.speaker_state;
  const category = payload.subject_category;
  const ageBand = payload.age_band;
  const serviceMode = payload.service_mode;
  if (speakerState === contracts.SpeakerState.Confirmed) {
    if (!isNonEmptyString(activeSubjectId)) {
      reasons.push("speaker_state=confirmed 必须携带 active_subject_id");
    }
  } else if (activeSubjectId !== null) {
    reasons.push("speaker_state 未确认时 active_subject_id 必须为 null");
  }
  if (CATEGORY_FOR_AGE_BAND[ageBand] !== category) {
    reasons.push("subject_category 与 age_band 关系非法");
  }
  const sensitiveGranted = payload.capabilities.some((capability) =>
    SENSITIVE_CAPABILITIES.includes(capability),
  );
  if (sensitiveGranted && speakerState !== contracts.SpeakerState.Confirmed) {
    reasons.push("unconfirmed/unknown 说话人携带敏感能力");
  }
  if (sensitiveGranted && category === contracts.SubjectCategory.Unknown) {
    reasons.push("subject_category=unknown 携带敏感能力");
  }
  if (category === contracts.SubjectCategory.Minor) {
    const adultCapability = payload.capabilities.find((capability) =>
      MINOR_FORBIDDEN_CAPABILITIES.includes(capability),
    );
    if (adultCapability !== undefined) {
      reasons.push(`minor 主体不允许能力 ${adultCapability}`);
    }
    if (MINOR_FORBIDDEN_MODES.includes(serviceMode)) {
      reasons.push(`minor 主体不允许服务模式 ${serviceMode}`);
    }
  }
  const unconfirmedOrUnknownCategory =
    speakerState !== contracts.SpeakerState.Confirmed ||
    category === contracts.SubjectCategory.Unknown;
  if (unconfirmedOrUnknownCategory && serviceMode !== contracts.ServiceMode.UnknownSafe) {
    reasons.push("未确认说话人/未知主体时 service_mode 必须是 unknown_safe");
  }
  if (serviceMode === contracts.ServiceMode.UnknownSafe) {
    const unexpectedCapability = payload.capabilities.find(
      (capability) => !UNKNOWN_SAFE_ALLOWED_CAPABILITIES.includes(capability),
    );
    if (unexpectedCapability !== undefined) {
      reasons.push(`unknown_safe 不允许能力 ${unexpectedCapability}`);
    }
    if (!payload.obligations.includes(contracts.PolicyObligation.DoNotPersist)) {
      reasons.push("unknown_safe 必须携带 DO_NOT_PERSIST 义务");
    }
  }

  if (reasons.length > 0) return failClosedRuntimeProfile(reasons);

  return {
    valid: true,
    degraded:
      serviceMode === contracts.ServiceMode.UnknownSafe ||
      speakerState !== contracts.SpeakerState.Confirmed,
    fail_reasons: [],
    signature_schema: payload.signature_schema,
    runtime_profile_id: payload.runtime_profile_id,
    device_id: payload.device_id,
    session_id: payload.session_id,
    actor_id: payload.actor_id,
    binding_id: payload.binding_id,
    binding_version: payload.binding_version,
    active_subject_id: activeSubjectId,
    subject_revision: payload.subject_revision,
    subject_category: category,
    age_band: ageBand,
    speaker_state: speakerState,
    speaker_confidence: confidence,
    service_mode: serviceMode,
    persona_assignment_id: payload.persona_assignment_id,
    persona,
    policy_bundle_version: payload.policy_bundle_version,
    capabilities: [...payload.capabilities],
    obligations: [...payload.obligations],
    policy_receipt_ids: [...payload.policy_receipt_ids],
    session_epoch: payload.session_epoch,
    issued_at: payload.issued_at,
    expires_at: payload.expires_at,
    signature: payload.signature,
  };
}

/* 去掉客户端附加字段，还原 canonical wire 载荷（供缓存持久化后重新校验）。 */
function runtimeProfileWirePayload(profile) {
  if (!isPlainObject(profile)) return null;
  const wire = {};
  for (const key of RUNTIME_PROFILE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(profile, key)) return null;
    wire[key] = profile[key];
  }
  return wire;
}

/*
 * 递归 key 排序的确定性序列化：用于 canonical 24 字段 + signature 的
 * 完整幂等比较。不能用 JSON.stringify(value, keys) 形式的 allowlist——
 * 它会递归过滤嵌套对象导致 persona 被序列化成 {}，掩盖内容差异。
 */
function canonicalJsonStringify(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJsonStringify).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJsonStringify(value[key])}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

function canonicalWireJson(profile) {
  const wire = runtimeProfileWirePayload(profile);
  return wire ? canonicalJsonStringify(wire) : "";
}

function hasCapability(profile, capability) {
  return Boolean(
    profile &&
      profile.valid === true &&
      Array.isArray(profile.capabilities) &&
      profile.capabilities.includes(capability),
  );
}

function sensitiveCapabilitiesFor(profile) {
  if (!profile || profile.valid !== true || !Array.isArray(profile.capabilities)) return [];
  return profile.capabilities.filter((capability) => SENSITIVE_CAPABILITIES.includes(capability));
}

/*
 * setActiveSubject / refresh 后只接受同一会话中 epoch 严格提升的新 profile。
 * 不同 session 一律视为不可比（必须重新解析），晚到/倒退响应由调用方丢弃。
 */
function isNewerRuntimeProfile(previous, next) {
  if (!next || next.valid !== true) return false;
  if (!previous || previous.valid !== true) return true;
  if (previous.session_id !== next.session_id) return false;
  return next.session_epoch > previous.session_epoch;
}

/*
 * 主体解析（§9.2 POST /v1/sessions/resolve-subject）响应规范化。
 * 未知 resolution 一律 unknown；候选主体只保留合法 person_id。
 */
function normalizeSubjectResolution(payload) {
  if (!isPlainObject(payload)) throw new TypeError("主体解析响应无效");
  const resolution = ["confirmed", "confirmation_required", "unknown"].includes(
    payload.resolution,
  )
    ? payload.resolution
    : "unknown";
  const candidates = (Array.isArray(payload.candidate_subjects)
    ? payload.candidate_subjects
    : []
  )
    .filter(
      (item) => isPlainObject(item) && typeof item.person_id === "string" && item.person_id,
    )
    .map((item) => ({
      person_id: item.person_id,
      display_name:
        typeof item.display_name === "string" && item.display_name ? item.display_name : "家庭成员",
      confidence: numberOrNull(item.confidence) ?? 0,
      relationship_label:
        typeof item.relationship_label === "string" ? item.relationship_label : "",
    }));
  return {
    resolution,
    candidate_subjects: candidates,
    temporary_service_mode: contracts.isServiceMode(payload.temporary_service_mode)
      ? payload.temporary_service_mode
      : contracts.SERVICE_MODE_DEFAULT,
    allowed_confirmation_methods: Array.isArray(payload.allowed_confirmation_methods)
      ? payload.allowed_confirmation_methods.filter((item) => typeof item === "string")
      : [],
    runtime_profile_id: stringOrNull(payload.runtime_profile_id),
  };
}

/* 敏感入口由服务端 capabilities 驱动（D-07）：能力未授予就不展示。 */
function sensitiveEntriesFor(runtimeProfile) {
  const capabilities =
    runtimeProfile && runtimeProfile.valid === true ? runtimeProfile.capabilities : [];
  return SENSITIVE_ENTRIES.filter((entry) => capabilities.includes(entry.capability));
}

/* unknown_safe / 未确认说话人的可解释降级说明（§4.3）。 */
function degradationFor(runtimeProfile) {
  if (!runtimeProfile || !runtimeProfile.degraded) return null;
  const reasons = [];
  if (runtimeProfile.valid === false) {
    reasons.push("服务端返回的 Runtime Profile 校验失败，敏感能力已关闭");
  }
  if (runtimeProfile.service_mode === contracts.ServiceMode.UnknownSafe) {
    reasons.push("当前对话处于安全模式（unknown_safe）");
  }
  if (runtimeProfile.speaker_state === contracts.SpeakerState.Unknown) {
    reasons.push("暂时无法确认是谁在使用这台设备");
  } else if (runtimeProfile.speaker_state === contracts.SpeakerState.Unconfirmed) {
    reasons.push("说话人尚未确认，敏感能力保持关闭");
  }
  if (reasons.length === 0) return null;
  return {
    reasons,
    allowed: ["普通聊天", "通用知识", "临时英语练习", "基础设备控制"],
    restricted: ["长期记忆", "学习进度", "私人历史检索", "家长摘要", "个性化敏感信息"],
    forbidden: ["声音复刻", "数字自我", "传承", "支付", "隐私设置变更", "查看他人记忆"],
  };
}

const BINDING_MANIFEST_KEY = "memoria:miniprogram:device:binding-manifest";
const RUNTIME_PROFILE_KEY = "memoria:miniprogram:device:runtime-profile";

/*
 * BindingManifest（§2.3 / schema BindingManifest）严格校验：与 canonical
 * 23 字段完全一致（无额外字段、无缺失）、枚举/版本/ID/时间/角色数组全部
 * 逐项校验。写入与每次读取都重新校验；本地篡改一律清理并视为未绑定。
 */
const BINDING_MANIFEST_KEYS = Object.freeze([
  "binding_id",
  "device_id",
  "declared_mode",
  "binding_version",
  "status",
  "reason",
  "supersedes_binding_id",
  "family_space_id",
  "account_owner_id",
  "device_admin_ids",
  "primary_subject_ids",
  "guardian_ids",
  "delegate_ids",
  "emergency_contact_ids",
  "member_ids",
  "roles",
  "service_profile_version",
  "policy_bundle_version",
  "consent_snapshot_id",
  "persona_assignment_id",
  "valid_from",
  "valid_until",
  "created_at",
]);
const BINDING_MANIFEST_KEY_SET = new Set(BINDING_MANIFEST_KEYS);
const BINDING_STATUS_VALUES = Object.freeze(["active", "superseded", "revoked", "expired"]);
const BINDING_REASON_VALUES = Object.freeze([
  "create",
  "supersede",
  "transfer",
  "unbind",
  "expire",
]);
const MANIFEST_ROLE_KEYS = Object.freeze(["person_id", "role", "permissions"]);
const MANIFEST_ID_ARRAYS = Object.freeze([
  "device_admin_ids",
  "primary_subject_ids",
  "guardian_ids",
  "delegate_ids",
  "emergency_contact_ids",
  "member_ids",
]);

function validateBindingManifest(payload) {
  const reasons = [];
  if (!isPlainObject(payload)) {
    return { valid: false, reasons: ["绑定清单不是对象"], manifest: null };
  }
  for (const key of Object.keys(payload)) {
    if (!BINDING_MANIFEST_KEY_SET.has(key)) reasons.push(`绑定清单存在未知字段 ${key}`);
  }
  for (const key of BINDING_MANIFEST_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(payload, key)) {
      reasons.push(`绑定清单缺少必需字段 ${key}`);
    }
  }
  if (reasons.length > 0) return { valid: false, reasons, manifest: null };
  for (const key of [
    "binding_id",
    "device_id",
    "account_owner_id",
    "service_profile_version",
    "policy_bundle_version",
  ]) {
    if (!isNonEmptyString(payload[key])) reasons.push(`${key} 必须是非空字符串（≤128）`);
  }
  if (!contracts.isDeviceDeclaredMode(payload.declared_mode)) {
    reasons.push(`declared_mode 取值无效：${payload.declared_mode}`);
  }
  if (!Number.isInteger(payload.binding_version) || payload.binding_version < 1) {
    reasons.push("binding_version 必须是 >= 1 的整数");
  }
  if (!BINDING_STATUS_VALUES.includes(payload.status)) {
    reasons.push(`status 取值无效：${payload.status}`);
  }
  if (!BINDING_REASON_VALUES.includes(payload.reason)) {
    reasons.push(`reason 取值无效：${payload.reason}`);
  }
  for (const key of [
    "supersedes_binding_id",
    "family_space_id",
    "consent_snapshot_id",
    "persona_assignment_id",
  ]) {
    if (payload[key] !== null && !isNonEmptyString(payload[key])) {
      reasons.push(`${key} 必须是字符串或 null`);
    }
  }
  for (const key of MANIFEST_ID_ARRAYS) {
    if (
      !isUniqueArray(payload[key], (item) => isNonEmptyString(item), 64)
    ) {
      reasons.push(`${key} 必须是去重的非空字符串数组（≤64）`);
    }
  }
  if (!Array.isArray(payload.roles) || payload.roles.length > 64) {
    reasons.push("roles 必须是数组（≤64）");
  } else {
    for (const role of payload.roles) {
      if (!isPlainObject(role)) {
        reasons.push("roles 项必须是对象");
        continue;
      }
      for (const key of Object.keys(role)) {
        if (!MANIFEST_ROLE_KEYS.includes(key)) reasons.push(`role 存在未知字段 ${key}`);
      }
      if (!isNonEmptyString(role.person_id)) reasons.push("role.person_id 非法");
      if (!contracts.isBindingRole(role.role)) reasons.push(`role.role 取值无效：${role.role}`);
      if (!isUniqueArray(role.permissions, (item) => isNonEmptyString(item, 64), 64)) {
        reasons.push("role.permissions 必须是去重的非空字符串数组（≤64）");
      }
    }
  }
  if (!isIsoDateTime(payload.valid_from)) reasons.push("valid_from 必须是 RFC3339 时间");
  if (payload.valid_until !== null && !isIsoDateTime(payload.valid_until)) {
    reasons.push("valid_until 必须是 RFC3339 时间或 null");
  }
  if (!isIsoDateTime(payload.created_at)) reasons.push("created_at 必须是 RFC3339 时间");
  if (reasons.length > 0) return { valid: false, reasons, manifest: null };
  if (
    payload.valid_until !== null &&
    parseRfc3339(payload.valid_until) <= parseRfc3339(payload.valid_from)
  ) {
    reasons.push("valid_until 必须晚于 valid_from");
  }
  if (reasons.length > 0) return { valid: false, reasons, manifest: null };
  return { valid: true, reasons: [], manifest: payload };
}

function storageAvailable() {
  try {
    return typeof wx !== "undefined" && typeof wx.getStorageSync === "function";
  } catch {
    return false;
  }
}

function readJsonStorage(key) {
  if (!storageAvailable()) return null;
  try {
    const value = wx.getStorageSync(key);
    if (!value || typeof value !== "object") return null;
    return value;
  } catch {
    return null;
  }
}

function saveBindingManifest(manifest) {
  const validated = validateBindingManifest(manifest);
  if (!validated.valid) throw new TypeError(`绑定结果无效：${validated.reasons[0]}`);
  const normalized = validated.manifest;
  if (!storageAvailable()) return;
  const previous = readBindingManifest();
  if (
    previous &&
    (previous.device_id !== normalized.device_id ||
      previous.binding_id !== normalized.binding_id ||
      previous.binding_version !== normalized.binding_version)
  ) {
    // 换设备 / 换绑定 / binding 版本变化：旧 Runtime Profile 一律失效。
    clearCachedRuntimeProfile();
  }
  wx.setStorageSync(BINDING_MANIFEST_KEY, normalized);
}

function readBindingManifest() {
  const stored = readJsonStorage(BINDING_MANIFEST_KEY);
  if (!stored) return null;
  const validated = validateBindingManifest(stored);
  if (!validated.valid) {
    // 本地清单被篡改/损坏：清理并视为未绑定，绝不凭部分字段开放能力。
    clearBindingManifest();
    return null;
  }
  return validated.manifest;
}

function clearBindingManifest() {
  if (!storageAvailable()) return;
  try {
    wx.removeStorageSync(BINDING_MANIFEST_KEY);
  } catch {
    // 清理失败不影响会话流程。
  }
}

/*
 * Runtime Profile 本地缓存。只能保存通过严格校验（valid=true）的 profile；
 * 缓存条目本身绑定 device_id + binding_id/binding_version + session_id，
 * 读取时必须四个上下文全部匹配，任何不匹配/过期/非法一律视为未命中并清理，
 * 防止跨 device/session/binding 复用。同一 session 内 session_epoch 单调，
 * 回退写入被拒绝（旧响应不能覆盖新响应）。
 */
function readRawRuntimeProfileCache() {
  return readJsonStorage(RUNTIME_PROFILE_KEY);
}

function saveCachedRuntimeProfile(profile) {
  if (!isPlainObject(profile) || profile.valid !== true) return false;
  if (!storageAvailable()) return false;
  const wire = runtimeProfileWirePayload(profile);
  if (!wire) return false;
  const previous = readRawRuntimeProfileCache();
  if (
    previous &&
    isPlainObject(previous.profile) &&
    previous.profile.session_id === wire.session_id &&
    typeof previous.profile.session_epoch === "number" &&
    wire.session_epoch <= previous.profile.session_epoch
  ) {
    return false; // 同会话 epoch 未提升：旧响应不得覆盖新响应。
  }
  try {
    wx.setStorageSync(RUNTIME_PROFILE_KEY, {
      profile: wire,
      saved_at: Date.now(),
    });
    return true;
  } catch {
    return false;
  }
}

function readCachedRuntimeProfile(context = {}, options = {}) {
  const { deviceId, bindingId, bindingVersion, sessionId } = context;
  const entry = readRawRuntimeProfileCache();
  if (!entry || !isPlainObject(entry.profile)) return null;
  if (
    typeof deviceId !== "string" ||
    typeof bindingId !== "string" ||
    typeof bindingVersion !== "number" ||
    typeof sessionId !== "string"
  ) {
    // 缺少完整上下文无法证明属于当前设备/绑定/会话：fail closed。
    clearCachedRuntimeProfile();
    return null;
  }
  const profile = normalizeRuntimeProfile(entry.profile, options);
  if (!profile.valid) {
    clearCachedRuntimeProfile();
    return null;
  }
  if (
    profile.device_id !== deviceId ||
    profile.binding_id !== bindingId ||
    profile.binding_version !== bindingVersion ||
    profile.session_id !== sessionId
  ) {
    // 跨 device/session/binding 一律不可复用，直接清理旧缓存。
    clearCachedRuntimeProfile();
    return null;
  }
  return profile;
}

function clearCachedRuntimeProfile() {
  if (!storageAvailable()) return;
  try {
    wx.removeStorageSync(RUNTIME_PROFILE_KEY);
  } catch {
    // 清理失败不影响会话流程。
  }
}

module.exports = {
  MODE_META,
  PRIMARY_RELATIONSHIPS,
  CONSENT_OFFERS,
  MODE_PREFERENCE_SCHEMAS,
  MODE_AGE_BANDS,
  AGE_BAND_LABELS,
  SENSITIVE_ENTRIES,
  SENSITIVE_CAPABILITIES,
  MINOR_FORBIDDEN_CAPABILITIES,
  MINOR_FORBIDDEN_MODES,
  buildBindingRequest,
  consentOffersFor,
  normalizeRuntimeProfile,
  normalizeSubjectResolution,
  runtimeProfileWirePayload,
  canonicalWireJson,
  hasCapability,
  sensitiveCapabilitiesFor,
  isNewerRuntimeProfile,
  sensitiveEntriesFor,
  degradationFor,
  entryForCapability,
  capabilityGateMessage,
  configActionGate,
  validateBindingManifest,
  saveBindingManifest,
  readBindingManifest,
  clearBindingManifest,
  saveCachedRuntimeProfile,
  readCachedRuntimeProfile,
  clearCachedRuntimeProfile,
};
