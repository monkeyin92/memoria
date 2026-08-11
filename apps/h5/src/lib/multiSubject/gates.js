import { Capability, ServiceMode } from "./contracts.js";
import { degradationFor } from "./runtimeProfile.js";

/**
 * 敏感入口与能力门禁（D-07 / §9.4）。
 *
 * 所有敏感入口只由“当前有效 profile.capabilities”驱动；Profile 不可用、
 * 503、离线、unknown_safe 时隐藏并给出可解释提示。绝不从出生年、关系或
 * 本地缓存推断权限。
 */
export const SENSITIVE_ENTRIES = Object.freeze([
  {
    key: "digital_self",
    capability: Capability.DigitalSelfPreview,
    title: "数字心智与声音",
    description: "人格学习、声纹识别与声音复刻",
    requiresConfirmedSubject: true,
  },
  {
    key: "speaker_enrollment",
    capability: Capability.VoiceProfileCreate,
    title: "主人声纹",
    description: "用自然、轻声、带笑和稳重语气各录一次",
    requiresConfirmedSubject: true,
  },
  {
    key: "raw_voice_consent",
    capability: Capability.RawAudioRetention,
    title: "隐私与数据",
    description: "查看或撤回原始语音归档授权",
    requiresConfirmedSubject: true,
  },
  {
    key: "memory_recall",
    capability: Capability.MemoryRecallPrivate,
    title: "私人回顾",
    description: "查看你的私人记忆",
    requiresConfirmedSubject: true,
  },
  {
    key: "guardian_summary",
    capability: Capability.GuardianSummaryView,
    title: "成长小结与监护授权",
    description: "查看已绑定孩子的每周聚合小结",
    requiresConfirmedSubject: true,
  },
]);

export function entryForCapability(capability) {
  return SENSITIVE_ENTRIES.find((entry) => entry.capability === capability) || null;
}

export function sensitiveCapabilitiesFor(profile) {
  if (!profile || profile.valid !== true || !Array.isArray(profile.capabilities)) {
    return [];
  }
  return profile.capabilities.filter(
    (capability) => SENSITIVE_ENTRIES.some((entry) => entry.capability === capability),
  );
}

function displayLockReason(profile, { offline = false, multipleSpeakers = false } = {}) {
  if (!profile || profile.valid !== true) return "invalid_profile";
  if (offline) return "offline";
  if (multipleSpeakers) return "multiple_speakers";
  if (profile.service_mode === ServiceMode.UnknownSafe) return "unknown_safe";
  if (profile.speaker_state === "unknown") return "speaker_unknown";
  if (profile.speaker_state === "unconfirmed") return "speaker_unconfirmed";
  if (profile.degraded === true) return "degraded";
  return null;
}

export function sensitiveEntryBlockReason(
  entry,
  profile,
  displayContext = {},
) {
  if (!entry) return "missing_entry";
  const displayReason = displayLockReason(profile, displayContext);
  if (entry.requiresConfirmedSubject && displayReason) return displayReason;
  if (!profile || profile.valid !== true) return displayReason || "invalid_profile";
  if (!Array.isArray(profile.capabilities) || !profile.capabilities.includes(entry.capability)) {
    return "capability_missing";
  }
  return null;
}

/**
 * 展示层红线（与“H5 不维护年龄/关系策略矩阵”不冲突）：
 * canonical 结构可以接受服务端签名的任意能力组合，但 UI 在
 * profile.degraded / service_mode=unknown_safe / speaker_state!=confirmed /
 * 离线 / 多人同时说话时，所有 requiresConfirmedSubject 的私人或敏感入口
 * 必须隐藏或拒绝——即使 capabilities 误含该值。普通 chat 不受影响。
 * 该判定不按年龄带/关系推断，只消费 profile 的 canonical 展示字段与
 * 明确的展示环境信号。
 */
export function isDegradedProfile(profile) {
  if (!profile || profile.valid !== true) return true;
  if (profile.degraded === true) return true;
  if (profile.service_mode === ServiceMode.UnknownSafe) return true;
  if (profile.speaker_state !== "confirmed") return true;
  return false;
}

export function isSensitiveEntryUsable(
  entry,
  profile,
  { offline = false, multipleSpeakers = false } = {},
) {
  return sensitiveEntryBlockReason(entry, profile, {
    offline,
    multipleSpeakers,
  }) === null;
}

/**
 * 敏感入口由服务端 capabilities 驱动 + 展示层红线收敛：
 * 能力未授予不展示；已授予但在降级/未确认/离线/多人状态下也不展示。
 */
export function sensitiveEntriesFor(profile, displayContext = {}) {
  if (!profile || profile.valid !== true) return [];
  return SENSITIVE_ENTRIES.filter((entry) =>
    isSensitiveEntryUsable(entry, profile, displayContext),
  );
}

/**
 * 敏感入口被拒绝时的可解释文案。reason 来自 requireRuntimeCapability
 * （no_binding / invalid_profile / capability_missing / superseded / unavailable）。
 */
export function capabilityGateMessage(result, capability) {
  const title = entryForCapability(capability)?.title || "该能力";
  if (result?.reason === "no_binding") {
    return "还没有绑定设备，无法取得 Runtime Profile；请先在「设备与成员」完成首次绑定。";
  }
  if (result?.reason === "no_session") {
    return "还没有进行中的语音会话，无法取得当前主体的能力；请先开始一次对话。";
  }
  if (result?.reason === "offline") {
    return "设备当前离线，暂时无法重新核验当前主体；请联网后再试。";
  }
  if (result?.reason === "multiple_speakers") {
    return "检测到多人同时说话，当前会话已进入 unknown_safe 安全模式，暂时不能开放该入口。";
  }
  if (result?.reason === "unknown_safe") {
    return "当前对话处于 unknown_safe 安全模式，敏感入口已关闭。";
  }
  if (result?.reason === "speaker_unknown") {
    return "暂时无法确认当前说话人，敏感入口已关闭。";
  }
  if (result?.reason === "speaker_unconfirmed") {
    return "说话人尚未确认，敏感入口已关闭。";
  }
  if (
    result?.reason === "invalid_profile" ||
    result?.reason === "unavailable"
  ) {
    return "服务端暂时无法提供有效的 Runtime Profile，此功能保持关闭。";
  }
  if (result?.reason === "superseded") {
    return "能力校验未通过，入口保持关闭，请刷新后重试。";
  }
  if (result?.reason === "capability_missing") {
    return `${title}尚未开放：当前主体未获得该能力。`;
  }
  return `${title}尚未开放：服务端未按当前主体授权。`;
}

export { degradationFor };
