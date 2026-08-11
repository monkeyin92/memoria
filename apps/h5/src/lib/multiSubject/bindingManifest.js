import { validateBindingManifest } from "./contracts.js";
import { normalizeRuntimeProfileV2 } from "./runtimeProfile.js";

/**
 * BindingManifest（§2.3）严格校验与本地缓存。
 *
 * 结构语义以 canonical validateBindingManifest 为唯一来源；写入与每次读取
 * 都重新校验。本地篡改/损坏一律清理并视为未绑定，绝不凭部分字段开放能力。
 */
const BINDING_MANIFEST_KEY = "memoria:h5:device:binding-manifest";
const RUNTIME_PROFILE_KEY = "memoria:h5:device:runtime-profile";

function storageAvailable() {
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.localStorage === "object" &&
      window.localStorage !== null
    );
  } catch {
    return false;
  }
}

function readJsonStorage(key) {
  if (!storageAvailable()) return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function writeJsonStorage(key, value) {
  if (!storageAvailable()) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // 缓存写入失败不影响会话流程。
  }
}

function removeJsonStorage(key) {
  if (!storageAvailable()) return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // 清理失败不影响会话流程。
  }
}

export function validateBindingManifestPayload(payload) {
  const reasons = validateBindingManifest(payload);
  if (reasons.length > 0) {
    return { valid: false, reasons, manifest: null };
  }
  return { valid: true, reasons: [], manifest: payload };
}

export function saveBindingManifest(manifest) {
  const validated = validateBindingManifestPayload(manifest);
  if (!validated.valid) {
    throw new TypeError(`绑定结果无效：${validated.reasons[0]}`);
  }
  const normalized = validated.manifest;
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
  writeJsonStorage(BINDING_MANIFEST_KEY, normalized);
  return normalized;
}

export function readBindingManifest() {
  const stored = readJsonStorage(BINDING_MANIFEST_KEY);
  if (!stored) return null;
  const validated = validateBindingManifestPayload(stored);
  if (!validated.valid) {
    // 本地清单被篡改/损坏：清理并视为未绑定。
    clearBindingManifest();
    return null;
  }
  return validated.manifest;
}

export function clearBindingManifest() {
  removeJsonStorage(BINDING_MANIFEST_KEY);
  clearCachedRuntimeProfile();
}

/**
 * Runtime Profile 本地缓存。只能保存通过严格校验（valid=true）的 profile；
 * 读取时必须 device_id + binding_id/binding_version + session_id 全部匹配，
 * 任何不匹配/过期/非法一律视为未命中并清理，防止跨 device/session/binding
 * 复用。同一 session 内 session_epoch 单调，回退写入被拒绝。
 */
export function saveCachedRuntimeProfile(profile) {
  if (!profile || profile.valid !== true) return false;
  if (!storageAvailable()) return false;
  const wire = runtimeProfileCacheWire(profile);
  if (!wire) return false;
  const previous = readJsonStorage(RUNTIME_PROFILE_KEY);
  if (
    previous &&
    previous.profile &&
    previous.profile.session_id === wire.session_id &&
    typeof previous.profile.session_epoch === "number" &&
    wire.session_epoch <= previous.profile.session_epoch
  ) {
    return false; // 同会话 epoch 未提升：旧响应不得覆盖新响应。
  }
  writeJsonStorage(RUNTIME_PROFILE_KEY, {
    profile: wire,
    saved_at: Date.now(),
  });
  return true;
}

function runtimeProfileCacheWire(profile) {
  if (!profile || typeof profile !== "object") return null;
  const allowed = [
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
  ];
  const wire = {};
  for (const key of allowed) {
    if (!Object.prototype.hasOwnProperty.call(profile, key)) return null;
    wire[key] = profile[key];
  }
  return wire;
}

export function readCachedRuntimeProfile(context = {}, options = {}) {
  const { deviceId, bindingId, bindingVersion, sessionId } = context;
  const entry = readJsonStorage(RUNTIME_PROFILE_KEY);
  if (!entry || !entry.profile || typeof entry.profile !== "object") return null;
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
  const profile = normalizeRuntimeProfileV2(entry.profile, options);
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

/**
 * 读取最近一次严格校验后写入的会话 profile（展示门禁降级回退）。
 *
 * 只消费“已经过 canonical 校验、上下文匹配、epoch 单调写入”的缓存条目：
 * 每次读取重新执行 canonical + 过期校验。绑定上下文（device/binding/
 * version）不匹配返回 null；校验失败（如已过期）返回 fail-closed 降级
 * 对象，由 UI 展示原因并隐藏敏感入口。无活跃会话时用它维持能力展示，
 * 不发起新的无 session 请求（严格性等同 getRuntimeProfile 的 refresh）。
 */
export function readLatestCachedRuntimeProfile(context = {}, options = {}) {
  const { deviceId, bindingId, bindingVersion } = context;
  if (
    typeof deviceId !== "string" ||
    typeof bindingId !== "string" ||
    typeof bindingVersion !== "number"
  ) {
    return null;
  }
  const entry = readJsonStorage(RUNTIME_PROFILE_KEY);
  if (!entry || !entry.profile || typeof entry.profile !== "object") return null;
  const profile = normalizeRuntimeProfileV2(entry.profile, options);
  if (profile.valid !== true) return profile;
  if (
    profile.device_id !== deviceId ||
    profile.binding_id !== bindingId ||
    profile.binding_version !== bindingVersion
  ) {
    return null;
  }
  return profile;
}

export function clearCachedRuntimeProfile() {
  removeJsonStorage(RUNTIME_PROFILE_KEY);
}
