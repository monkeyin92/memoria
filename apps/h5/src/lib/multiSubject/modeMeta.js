import {
  DEVICE_DECLARED_MODE_VALUES,
  RelationshipType,
} from "./contracts.js";

/**
 * 首次绑定四分流（整改文档 §2）。
 *
 * 模式键只来自 canonical DEVICE_DECLARED_MODE_VALUES，H5 不定义第二套
 * 枚举；卡片顺序、标题、文案在此映射，请求序列化时仍以 canonical 值为准
 * （见 bindingRequest.js 的 isDeviceDeclaredMode 校验）。
 */
export const MODE_TITLES = Object.freeze({
  parent_for_child: "给孩子使用",
  self_use: "给自己使用",
  child_for_parent: "给父母使用",
  family_shared: "家庭共同使用",
});

const MODE_TAGLINES = Object.freeze({
  parent_for_child: "学习、陪伴与成长记录",
  self_use: "专属你的私人陪伴",
  child_for_parent: "管理归你，内容归父母本人",
  family_shared: "全家共用，每人独立档案",
});

const MODE_DESCRIPTIONS = Object.freeze({
  parent_for_child:
    "将建立儿童主体与监护关系；学习、时长与监护规则由服务端统一管控，人格不会覆盖儿童策略。",
  self_use:
    "建立本人主体；记忆、声音、数字自我等能力分别授权，敏感能力默认关闭。",
  child_for_parent:
    "区分设备管理员与实际使用者；子女可协助管理设备，但不能默认读取父母私人内容，父母本人接受后才能开启本人记录。",
  family_shared:
    "建立家庭空间与成员独立档案；私人记忆、家庭共享记忆与待确认记忆分区管理，无法确认说话人时安全降级。",
});

export const MODE_CARDS = DEVICE_DECLARED_MODE_VALUES.map((mode) => ({
  mode,
  title: MODE_TITLES[mode],
  tagline: MODE_TAGLINES[mode],
  description: MODE_DESCRIPTIONS[mode],
}));

/**
 * primary_subject.relationship 与模式的对应（canonical RelationshipType）。
 * 客户端不自行发明取值，请求序列化时逐项校验。
 */
export const PRIMARY_RELATIONSHIPS = Object.freeze({
  parent_for_child: RelationshipType.GuardianOf,
  self_use: RelationshipType.Self,
  child_for_parent: RelationshipType.ChildOf,
  family_shared: RelationshipType.FamilyMemberOf,
});

/**
 * ConsentOffer 目录（PR-04：服务端必须验证 offer id，客户端只提交给用户
 * 展示过且可代为确认的 offer）。requiresParentSelfAcceptance 的 offer
 * 只能由父母本人在设备上确认，客户端不得写入 consent_offer_ids ——
 * buildBindingRequest 会直接抛错（fail-closed），UI 也只展示为待父母确认。
 */
export const CONSENT_OFFERS = Object.freeze([
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

export const OFFER_BY_ID = Object.freeze(
  CONSENT_OFFERS.reduce((map, offer) => {
    map[offer.id] = offer;
    return map;
  }, {}),
);

/**
 * service_preferences 白名单（按 canonical 模式）。未知键或非法值一律
 * 抛错，防止通过偏好字段夹带策略/授权信息。
 */
export const MODE_PREFERENCE_SCHEMAS = Object.freeze({
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

/** 各模式的年龄带展示选项（仅 UI 展示与默认文案，不写入绑定请求）。 */
export const MODE_AGE_BANDS = Object.freeze({
  parent_for_child: ["under_14", "14_17"],
  child_for_parent: ["adult"],
  self_use: ["adult"],
  family_shared: [],
});

export const AGE_BAND_LABELS = Object.freeze({
  unknown: "未确认",
  under_14: "14 岁以下",
  "14_17": "14 至 17 岁",
  adult: "18 岁及以上",
});

export function consentOffersFor(mode) {
  return CONSENT_OFFERS.filter((offer) => offer.modes.includes(mode)).map((offer) => ({
    id: offer.id,
    label: offer.label,
    description: offer.description,
    defaultChecked: offer.defaultChecked,
    requiresParentSelfAcceptance: offer.requiresParentSelfAcceptance,
  }));
}
