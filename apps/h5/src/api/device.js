import { request } from "./client.js";
import { createSession } from "./session.js";
import { buildBindingRequest } from "../lib/multiSubject/bindingRequest.js";
import {
  clearCachedRuntimeProfile,
  readCachedRuntimeProfile,
  readBindingManifest,
  saveBindingManifest,
  saveCachedRuntimeProfile,
  validateBindingManifestPayload,
} from "../lib/multiSubject/bindingManifest.js";
import { normalizeRuntimeProfileV2 } from "../lib/multiSubject/runtimeProfile.js";
import { normalizeSubjectResolution } from "../lib/multiSubject/subjectResolution.js";

/**
 * H5 多用户/设备 API adapter（整改文档 §9 冻结端点）。
 *
 * - 请求体先经 buildBindingRequest fail-closed 校验（客户端不提交
 *   policy_version 或任何假授权字段）；
 * - 响应以 canonical 生成校验器为唯一语义来源（清单 / SubjectResolution /
 *   RuntimeProfileSignedV2），H5 不维护客户端 Policy 矩阵；
 * - Runtime Profile 请求带“请求代次”：晚到响应（被更新的请求取代）一律
 *   丢弃返回 null；切换主体必须拿到同一会话中 session_epoch 严格提升的
 *   新 profile（refresh 允许同 epoch，但拒绝倒退）；
 * - 敏感 Profile 一律要求先创建真实会话（sessionId 必填），无会话即
 *   fail closed，不做“无 session 的半校验”。
 */

/** 创建设备绑定（§9.1）。请求体经 buildBindingRequest fail-closed 校验。 */
export async function createDeviceBinding(requestBody, { idempotencyKey = "" } = {}) {
  const payload = buildBindingRequest(requestBody);
  const previous = readBindingManifest();
  const headers = idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {};
  const manifest = await request("/v1/device-bindings", {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  const saved = saveBindingManifest(manifest);
  if (
    !previous ||
    previous.device_id !== saved.device_id ||
    previous.binding_id !== saved.binding_id ||
    previous.binding_version !== saved.binding_version
  ) {
    // 重新绑定/绑定版本变化：旧 Runtime Profile 与内存守卫一律失效。
    clearCachedRuntimeProfile();
    clearRuntimeProfileMemory();
  }
  return saved;
}

/** 查询设备当前绑定（PR-04）。响应必须通过 canonical 校验才可缓存/展示。 */
export function getDeviceBinding(deviceId) {
  return request(`/v1/devices/${encodeURIComponent(deviceId)}/binding`).then(
    (payload) => {
      const validated = validateBindingManifestPayload(payload);
      if (!validated.valid) {
        const error = new Error(`设备绑定响应校验失败：${validated.reasons[0]}`);
        error.status = 502;
        throw error;
      }
      return saveBindingManifest(validated.manifest);
    },
  );
}

/** 解析当前会话主体（§9.2）。客户端不发送声纹原始数据。 */
export function resolveSessionSubject({
  deviceId,
  sessionId = null,
  clientClaimedPersonId = null,
  environment = {},
} = {}) {
  return request("/v1/sessions/resolve-subject", {
    method: "POST",
    body: JSON.stringify({
      device_id: deviceId,
      session_id: sessionId,
      client_claimed_person_id: clientClaimedPersonId,
      environment,
    }),
  }).then((payload) => normalizeSubjectResolution(payload));
}

/*
 * Runtime Profile 请求代次 + 会话 epoch floor：任何新请求都会使旧请求
 * 失效，晚到响应由 isRuntimeProfileRequestCurrent 判定后丢弃。epoch floor
 * 记录每个 session 已接受的最新 epoch，防止倒退响应被保存。
 */
let runtimeProfileRequestToken = 0;
const runtimeProfileEpochFloor = new Map();

function beginRuntimeProfileRequest() {
  runtimeProfileRequestToken += 1;
  return runtimeProfileRequestToken;
}

function isRuntimeProfileRequestCurrent(token) {
  return token === runtimeProfileRequestToken;
}

function clearRuntimeProfileMemory() {
  runtimeProfileRequestToken += 1;
  runtimeProfileEpochFloor.clear();
}

function requireActiveBinding() {
  const manifest = readBindingManifest();
  if (!manifest || typeof manifest.device_id !== "string") {
    const error = new Error("还没有绑定设备，无法获取 Runtime Profile。");
    error.status = 403;
    throw error;
  }
  return manifest;
}

function acceptRuntimeProfile(profile, rawPayload, context, { mode, previous } = {}) {
  if (!profile) return null;
  // 无效 profile（fail-closed 降级对象）身份字段被置空，须用原始载荷
  // 完成 device/binding/session 上下文比对。
  const identitySource = profile.valid === true ? profile : rawPayload;
  if (
    typeof context.deviceId !== "string" ||
    typeof context.bindingId !== "string" ||
    typeof context.bindingVersion !== "number" ||
    typeof context.sessionId !== "string" ||
    !context.sessionId ||
    !identitySource ||
    identitySource.device_id !== context.deviceId ||
    identitySource.binding_id !== context.bindingId ||
    identitySource.binding_version !== context.bindingVersion ||
    identitySource.session_id !== context.sessionId
  ) {
    // 响应与当前经过校验的 BindingManifest/会话上下文不一致：fail closed。
    return null;
  }
  if (profile.valid !== true) {
    // fail-closed 降级对象（过期/校验失败）：refresh 路径返回给调用方
    // 展示可解释原因；switch 路径不允许把无效 profile 当作切换结果。
    return mode === "switch" ? null : profile;
  }
  const floor = runtimeProfileEpochFloor.get(context.sessionId);
  if (mode === "switch") {
    // 切换必须持有切换前有效 profile 且 epoch 严格前进。
    if (!previous || previous.valid !== true) return null;
    if (profile.session_epoch <= previous.session_epoch) return null;
    if (floor !== undefined && profile.session_epoch <= floor) return null;
  } else if (floor !== undefined && profile.session_epoch < floor) {
    // refresh：同 epoch 可接受（幂等重取），倒退拒绝。
    return null;
  }
  runtimeProfileEpochFloor.set(context.sessionId, profile.session_epoch);
  return profile;
}

/**
 * 获取 Runtime Profile（§9.3）。敏感 Profile 必须绑定真实会话：
 * sessionId 缺失直接 fail closed（抛 409 可解释错误）。响应经 canonical
 * 严格校验；晚到响应、epoch 倒退、或 device/session/binding 与当前
 * BindingManifest 不一致时返回 null，由调用方丢弃。
 */
export async function getRuntimeProfile(deviceId, { sessionId } = {}) {
  const manifest = requireActiveBinding();
  if (manifest.device_id !== deviceId) {
    const error = new Error("设备与当前绑定不一致，请刷新后重试。");
    error.status = 409;
    throw error;
  }
  if (typeof sessionId !== "string" || !sessionId) {
    const error = new Error("请先开始一次语音对话，再获取当前主体的能力。");
    error.status = 409;
    throw error;
  }
  const context = {
    deviceId: manifest.device_id,
    bindingId: manifest.binding_id,
    bindingVersion: manifest.binding_version,
    sessionId,
  };
  const token = beginRuntimeProfileRequest();
  const query = `?session_id=${encodeURIComponent(sessionId)}`;
  const payload = await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/runtime-profile${query}`,
  );
  if (!isRuntimeProfileRequestCurrent(token)) return null;
  const profile = normalizeRuntimeProfileV2(payload);
  if (!isRuntimeProfileRequestCurrent(token)) return null;
  const accepted = acceptRuntimeProfile(profile, payload, context, {
    mode: "refresh",
  });
  if (accepted === null) return null;
  saveCachedRuntimeProfile(accepted);
  return accepted;
}

/**
 * 切换当前使用者（§9.5）。confirmation_method 只允许服务端声明的确认方式
 * （如 app_confirm），客户端不能自行断言已通过声纹确认。受请求代次与
 * session_epoch 严格单调守卫：必须持有切换前 profile 且新 profile epoch
 * 严格提升，否则返回 null；晚到/倒退响应一律丢弃。
 */
export async function setActiveSubject(
  sessionId,
  { personId, confirmationMethod = "app_confirm" } = {},
) {
  const manifest = requireActiveBinding();
  if (typeof sessionId !== "string" || !sessionId) {
    const error = new Error("请先开始一次语音对话，再切换当前使用者。");
    error.status = 409;
    throw error;
  }
  const context = {
    deviceId: manifest.device_id,
    bindingId: manifest.binding_id,
    bindingVersion: manifest.binding_version,
    sessionId,
  };
  const previous = readCachedRuntimeProfile(context);
  if (!previous || previous.valid !== true) {
    const error = new Error("还没有可验证的当前会话 Profile，无法安全切换使用者。");
    error.status = 409;
    throw error;
  }
  const token = beginRuntimeProfileRequest();
  const payload = await request(
    `/v1/sessions/${encodeURIComponent(sessionId)}/active-subject`,
    {
      method: "POST",
      body: JSON.stringify({
        person_id: personId,
        confirmation_method: confirmationMethod,
      }),
    },
  );
  if (!isRuntimeProfileRequestCurrent(token)) return null;
  const profile = normalizeRuntimeProfileV2(payload);
  if (!isRuntimeProfileRequestCurrent(token)) return null;
  const accepted = acceptRuntimeProfile(profile, payload, context, {
    mode: "switch",
    previous,
  });
  if (accepted === null) return null;
  saveCachedRuntimeProfile(accepted);
  return accepted;
}

/**
 * 敏感入口的统一能力门禁（D-07）：只有当前设备取得有效 Runtime Profile
 * 且 capabilities 包含目标能力时才放行；Profile 不可用（未绑定/无会话/
 * 503/离线/校验失败）时返回可解释拒绝，reason 含 no_session。所有敏感
 * 入口必须经此门禁，绝不从年龄/关系/本地缓存推断权限。
 */
export async function requireRuntimeCapability(capability, { sessionId = null } = {}) {
  let binding = null;
  try {
    binding = readBindingManifest();
  } catch {
    binding = null;
  }
  if (!binding || typeof binding.device_id !== "string") {
    return { allowed: false, reason: "no_binding", profile: null };
  }
  if (typeof sessionId !== "string" || !sessionId) {
    return { allowed: false, reason: "no_session", profile: null };
  }
  try {
    const profile = await getRuntimeProfile(binding.device_id, { sessionId });
    if (profile === null) {
      return { allowed: false, reason: "superseded", profile: null };
    }
    if (profile.valid !== true) {
      return { allowed: false, reason: "invalid_profile", profile };
    }
    if (!profile.capabilities.includes(capability)) {
      return { allowed: false, reason: "capability_missing", profile };
    }
    return { allowed: true, reason: "allowed", profile };
  } catch (error) {
    return { allowed: false, reason: "unavailable", profile: null, error };
  }
}

/**
 * 创建设备管理会话（POST /v1/sessions 的严格消费者）。
 *
 * 消费其 canonical 响应：会话必须携带签名 RuntimeProfileSignedV2
 * （session.runtime_profile），且与本次创建的 session_id / 当前
 * BindingManifest 上下文一致；actor_id（账号）与 active_subject_id（主体）
 * 分离消费，H5 门禁只认 capabilities 与 active_subject，绝不把 actor 当
 * 主体。profile 缺失、校验失败、旧 v1 schema、503 一律 fail closed。
 */
export async function createDeviceSession(userId, options = {}) {
  const manifest = requireActiveBinding();
  const session = await createSession(userId, options.voiceBackend || "cascade", null, {
    interactionMode: "companion",
  });
  if (!session || typeof session.session_id !== "string" || !session.session_id) {
    throw new Error("服务端没有返回可验证的会话标识，会话已停止");
  }
  const payload = session.runtime_profile;
  if (!payload || typeof payload !== "object") {
    throw new Error("会话响应缺少签名 Runtime Profile，能力保持关闭");
  }
  const profile = normalizeRuntimeProfileV2(payload);
  if (profile.valid !== true) {
    throw new Error(
      `会话签名 Profile 校验失败（${profile.fail_reasons[0]}），能力保持关闭`,
    );
  }
  const context = {
    deviceId: manifest.device_id,
    bindingId: manifest.binding_id,
    bindingVersion: manifest.binding_version,
    sessionId: session.session_id,
  };
  const accepted = acceptRuntimeProfile(profile, payload, context, {
    mode: "refresh",
  });
  if (accepted === null) {
    throw new Error("会话 Profile 与当前设备/绑定上下文不一致，能力保持关闭");
  }
  if (typeof accepted.actor_id !== "string" || !accepted.actor_id) {
    throw new Error("会话 Profile 缺少账号身份（actor_id），能力保持关闭");
  }
  saveCachedRuntimeProfile(accepted);
  return { session, profile: accepted };
}
