import {
  SERVICE_MODE_DEFAULT,
  ServiceMode,
  SpeakerState,
  SubjectCategory,
  validateRuntimeProfileSignedV2,
} from "./contracts.js";

/**
 * RuntimeProfileSignedV2（§9.3 / D-07）严格 fail-closed 规范化。
 *
 * 结构语义（未知字段、缺必填、枚举、签名形状、session_epoch>=1）以
 * canonical 生成校验器 validateRuntimeProfileSignedV2 为唯一来源；本模块
 * 只补充 schema 无法表达的“时间”不变量（过期 / 未来签发时间）。
 *
 * 刻意不在 H5 维护任何客户端 Policy 矩阵：不按年龄带/关系/服务模式推断
 * 能力（MINOR_FORBIDDEN_*、unknown_safe 白名单、category/age_band 映射等
 * 一律不在此处），跨字段授权语义由服务端签名 profile 与生成合同负责，
 * UI 只消费 capabilities 逐入口门禁。
 *
 * 任何一项不通过整体 fail closed：返回 valid=false 的降级 profile
 * （unknown_safe + 空 capabilities），由 UI 给出可解释提示。客户端不持有
 * HMAC 密钥，只做 TLS 响应边界 + 信封结构校验（signature 必须 hex64 形状，
 * 由 canonical 校验器覆盖）。
 */
const HEX64_PATTERN = /^[a-f0-9]{64}$/;

/**
 * 严格 RFC3339/ISO-8601 校验：必须携带时区，拒绝 Date.parse 宽松解析。
 * 同时校验日历真实性与时区偏移边界，防止滚动日期/24:00 混入时间语义。
 */
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
  const utcMs =
    Date.UTC(year, month - 1, day, hour, minute, second) - offsetMinutes * 60_000;
  const wall = new Date(utcMs + offsetMinutes * 60_000);
  if (Number.isNaN(wall.getTime())) return null;
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

export function isRfc3339DateTime(value) {
  return parseRfc3339(value) !== null;
}

function failClosedRuntimeProfile(reasons) {
  return {
    valid: false,
    degraded: true,
    fail_reasons: reasons,
    signature_schema: "runtime-profile-v2",
    runtime_profile_id: null,
    device_id: null,
    session_id: null,
    actor_id: null,
    binding_id: null,
    binding_version: null,
    active_subject_id: null,
    subject_revision: null,
    subject_category: SubjectCategory.Unknown,
    age_band: "unknown",
    speaker_state: SpeakerState.Unknown,
    speaker_confidence: null,
    service_mode: SERVICE_MODE_DEFAULT,
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

/**
 * 规范化服务端返回的 RuntimeProfileSignedV2。options.now 仅供测试注入。
 * 返回 { valid, degraded, ...wire }；valid=false 时保持 fail-closed 降级
 * 形状，调用方必须据此隐藏全部敏感入口。
 */
export function normalizeRuntimeProfileV2(payload, options = {}) {
  const now = options.now !== undefined ? options.now : Date.now();
  const reasons = [...validateRuntimeProfileSignedV2(payload)];
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

  if (reasons.length > 0) return failClosedRuntimeProfile(reasons);

  const serviceMode = payload.service_mode;
  return {
    valid: true,
    degraded:
      serviceMode === ServiceMode.UnknownSafe ||
      payload.speaker_state !== SpeakerState.Confirmed,
    fail_reasons: [],
    ...payload,
    capabilities: [...payload.capabilities],
    obligations: [...payload.obligations],
    policy_receipt_ids: [...payload.policy_receipt_ids],
  };
}

/** 去掉客户端附加字段，还原 canonical wire 载荷（供缓存持久化后重新校验）。 */
export function runtimeProfileWirePayload(profile) {
  if (!profile || typeof profile !== "object") return null;
  const allowed = new Set([
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
  const wire = {};
  for (const key of allowed) {
    if (!Object.prototype.hasOwnProperty.call(profile, key)) return null;
    wire[key] = profile[key];
  }
  return wire;
}

export function hasCapability(profile, capability) {
  return Boolean(
    profile &&
      profile.valid === true &&
      Array.isArray(profile.capabilities) &&
      profile.capabilities.includes(capability),
  );
}

/**
 * 同一会话中 session_epoch 严格提升才接受新 profile；不同 session 不可比
 * （必须重新解析）。晚到/倒退响应由调用方据此丢弃。
 */
export function isNewerRuntimeProfile(previous, next) {
  if (!next || next.valid !== true) return false;
  if (!previous || previous.valid !== true) return true;
  if (previous.session_id !== next.session_id) return false;
  return next.session_epoch > previous.session_epoch;
}

/**
 * unknown_safe / 未确认说话人的可解释降级说明（§4.3）。
 */
export function degradationFor(runtimeProfile, displayContext = {}) {
  if (
    !runtimeProfile ||
    (!runtimeProfile.degraded &&
      !displayContext.offline &&
      !displayContext.multipleSpeakers)
  ) {
    return null;
  }
  const reasons = [];
  if (runtimeProfile.valid === false) {
    reasons.push("服务端返回的 Runtime Profile 校验失败，敏感能力已关闭");
  }
  if (displayContext.offline) {
    reasons.push("设备当前离线，暂时无法核验当前主体");
  }
  if (displayContext.multipleSpeakers) {
    reasons.push("检测到多人同时说话，已进入 unknown_safe 安全模式");
  }
  if (runtimeProfile.service_mode === ServiceMode.UnknownSafe) {
    reasons.push("当前对话处于安全模式（unknown_safe）");
  }
  if (runtimeProfile.speaker_state === SpeakerState.Unknown) {
    reasons.push("暂时无法确认是谁在使用这台设备");
  } else if (runtimeProfile.speaker_state === SpeakerState.Unconfirmed) {
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

/** 签名信封形状校验（H5 不持有 HMAC 密钥，hex64 形状由 canonical 校验器覆盖）。 */
export function isValidSignatureShape(signature) {
  return typeof signature === "string" && HEX64_PATTERN.test(signature);
}
