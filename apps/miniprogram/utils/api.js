const { CONTROL_API_BASE_URL } = require("../config");
const { normalizeGuardianLinks, normalizeGuardianSummary } = require("./guardian");
const {
  buildBindingRequest,
  normalizeRuntimeProfile,
  normalizeSubjectResolution,
  readBindingManifest,
  saveBindingManifest,
  saveCachedRuntimeProfile,
  clearCachedRuntimeProfile,
  canonicalWireJson,
} = require("./device-binding");
const {
  normalizeIntrospectResponse,
  normalizeOnboardingSession,
  normalizeClaimResponse,
  normalizeActivationResponse,
} = require("./device-onboarding/contracts");

/*
 * Runtime Profile 请求代次与 epoch 守卫（§9.5 / §10.2，复审 P0-2/3/4）：
 * - 完整 context = device_id + binding_id + binding_version + session_id，
 *   与经过严格校验的 BindingManifest 对齐；代次按 context 隔离，不同
 *   binding/session 的并行请求互不取消，同一 context 内晚到响应丢弃；
 * - 响应必须先通过 expected context 校验（device/binding/version/session
 *   与请求及当前 BindingManifest 一致），任何 mismatch 拒绝并清理缓存，
 *   防止另一个 device/binding 的有效 profile 被复用放行；
 * - 幂等比较是 canonical 全 24 字段 + signature 的确定性完整比较：普通
 *   refresh 只接受“同 session + 同 epoch + 完全相同的 wire 内容”；
 *   同 epoch 任何字段（capabilities/obligations/speaker_state/service_mode/
 *   persona/expiry/signature 等）变化都拒绝；
 * - setActiveSubject 成功结果必须严格 epoch 提升；
 * - 内存态（请求代次 + epoch floor）在登出 / 重新绑定 / 401 时清理。
 */
const runtimeProfileSeqs = new Map();
const acceptedProfileFloors = new Map(); // contextKey -> { epoch, fingerprint }

function runtimeProfileContextKey(context) {
  return [
    context.deviceId || "",
    context.bindingId || "",
    context.bindingVersion ?? "",
    context.sessionId || "",
  ].join("|");
}

function beginRuntimeProfileRequest(context) {
  const key = runtimeProfileContextKey(context);
  const seq = (runtimeProfileSeqs.get(key) || 0) + 1;
  runtimeProfileSeqs.set(key, seq);
  return { key, seq };
}

function isRuntimeProfileRequestCurrent(token) {
  return runtimeProfileSeqs.get(token.key) === token.seq;
}

function profileIdentityFingerprint(profile) {
  // canonical 全 24 字段 + signature 的递归确定性完整比较（P0-2）。
  return canonicalWireJson(profile);
}

/*
 * 清理全部内存守卫状态（请求代次 + epoch floor）。登出、绑定上下文变化
 * （重新绑定/版本变化）与鉴权 401 时调用，避免旧 epoch floor 或请求
 * 代次残留导致新上下文被错误拒绝/放行。
 */
function clearRuntimeProfileMemory() {
  runtimeProfileSeqs.clear();
  acceptedProfileFloors.clear();
}

function profileMatchesExpectedContext(profile, context) {
  if (!context) return false;
  if (profile.device_id !== context.deviceId) return false;
  if (profile.binding_id !== context.bindingId) return false;
  if (profile.binding_version !== context.bindingVersion) return false;
  if (context.sessionId && profile.session_id !== context.sessionId) return false;
  return true;
}

function acceptRuntimeProfile(profile, mode, context) {
  if (!profile || profile.valid !== true) return profile;
  if (!profileMatchesExpectedContext(profile, context)) {
    // 跨 device/binding/session 响应：拒绝并清理缓存（P0-3）。
    clearCachedRuntimeProfile();
    return null;
  }
  // epoch floor 按响应中的真实 session 归属，保证无 session 的 refresh
  // 与同 session 的 switch 共用同一把 epoch 锁。
  const floorKey = runtimeProfileContextKey({
    deviceId: profile.device_id,
    bindingId: profile.binding_id,
    bindingVersion: profile.binding_version,
    sessionId: profile.session_id,
  });
  const previous = acceptedProfileFloors.get(floorKey);
  const fingerprint = profileIdentityFingerprint(profile);
  if (previous) {
    if (profile.session_epoch < previous.epoch) return null;
    if (profile.session_epoch === previous.epoch) {
      if (mode === "switch") return null; // 切换必须严格提升
      if (fingerprint !== previous.fingerprint) return null; // 同 epoch 内容必须完全一致
    }
  }
  acceptedProfileFloors.set(floorKey, {
    epoch: profile.session_epoch,
    fingerprint,
  });
  return profile;
}

class ApiError extends Error {
  constructor(message, { status = 0, code = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function currentApp() {
  try {
    return getApp();
  } catch {
    return null;
  }
}

function currentIdentity() {
  return currentApp()?.globalData?.identity || null;
}

function currentAccessToken() {
  const app = currentApp();
  const token = app?.globalData?.accessToken || "";
  const expiresAt = Number(app?.globalData?.accessTokenExpiresAt || 0);
  return token && expiresAt > Date.now() + 5000 ? token : "";
}

function hasAuthenticatedSession() {
  return Boolean(currentIdentity() && currentAccessToken());
}

function currentAuthEpoch() {
  return Number(currentApp()?.globalData?.authEpoch || 0);
}

function isAuthEpochCurrent(epoch) {
  return epoch === currentAuthEpoch() && hasAuthenticatedSession();
}

function setAuthenticatedIdentity(response) {
  const app = currentApp();
  if (!app) throw new Error("小程序尚未初始化");
  return app.setAuthenticatedIdentity(response);
}

function requireAuthenticatedIdentity() {
  const identity = currentIdentity();
  if (!identity || !currentAccessToken()) {
    throw new ApiError("请先使用微信登录。", { status: 401 });
  }
  return identity;
}

function errorFromResponse(response) {
  const body = response.data;
  const detail = body && typeof body === "object" ? body.detail : null;
  const code =
    detail && typeof detail === "object" && typeof detail.code === "string"
      ? detail.code
      : null;
  const message =
    typeof detail === "string"
      ? detail
      : code === "phone_authorization_required"
        ? "请授权手机号完成登录。"
        : code === "wechat_credentials_missing"
          ? "微信登录服务尚未配置，请稍后再试。"
          : code === "wechat_identity_conflict"
            ? "当前微信身份与已绑定手机号不一致，请联系客服处理。"
            : code === "account_deletion_in_progress"
              ? "账号正在注销处理中，暂时无法重新登录。"
              : code && code.startsWith("wechat_")
                ? "微信登录校验未完成，请重新授权后再试。"
                : code === "guardian_summary_projection_unavailable"
                    ? "成长小结服务正在准备中。"
                  : code === "guardian_link_required"
                      ? "你还没有查看这份成长小结的权限。"
                      : code === "guardian_consent_required"
                        ? "这份成长小结尚未获得授权。"
                        : code === "guardian_wechat_identity_required"
                          ? "请先完成微信身份登录，再管理监护关系。"
                          : code === "guardian_link_confirmation_rejected"
                            ? "绑定码无效、已过期或年龄段不符合要求。"
                            : code === "guardian_consent_conflict"
                              ? "这项授权已经存在，请刷新后再试。"
                              : code === "minor_forbidden"
                                ? "学生账号不开放这项能力。"
                  : response.statusCode === 429
                    ? "操作太频繁，请稍后再试。"
                    : `请求未完成（${response.statusCode || 0}）`;
  return new ApiError(message, { status: response.statusCode || 0, code });
}

function rawRequest(path, options = {}) {
  const {
    method = "GET",
    data,
    authenticated = true,
    idempotencyKey = "",
  } = options;
  if (authenticated) requireAuthenticatedIdentity();
  const headers = {
    Accept: "application/json",
    ...(data === undefined ? {} : { "content-type": "application/json" }),
    ...(authenticated ? { Authorization: `Bearer ${currentAccessToken()}` } : {}),
    ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
  };
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${CONTROL_API_BASE_URL}${path}`,
      method,
      data,
      header: headers,
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data);
          return;
        }
        const error = errorFromResponse(response);
        if (authenticated && response.statusCode === 401) {
          // 登录态失效：本地的 Runtime Profile 缓存一并失效。
          clearCachedRuntimeProfile();
          clearRuntimeProfileMemory();
          currentApp()?.clearAuthenticatedIdentity?.();
        }
        reject(error);
      },
      fail(error) {
        reject(new ApiError(error?.errMsg || "网络连接失败，请稍后重试。"));
      },
    });
  });
}

function wechatLoginCode() {
  return new Promise((resolve, reject) => {
    wx.login({
      success(result) {
        if (result?.code) resolve(result.code);
        else reject(new ApiError("微信登录凭证获取失败。"));
      },
      fail(error) {
        reject(new ApiError(error?.errMsg || "微信登录凭证获取失败。"));
      },
    });
  });
}

async function requestWechatIdentity({ phoneCode, displayName } = {}) {
  const loginCode = await wechatLoginCode();
  const response = await rawRequest("/v1/auth/wechat-login", {
    method: "POST",
    data: {
      login_code: loginCode,
      ...(phoneCode ? { phone_code: phoneCode } : {}),
      ...(displayName?.trim() ? { display_name: displayName.trim() } : {}),
    },
    authenticated: false,
  });
  return setAuthenticatedIdentity(response);
}

let restorePromise = null;

function restoreWechatIdentity() {
  if (hasAuthenticatedSession()) return Promise.resolve(currentIdentity());
  if (restorePromise) return restorePromise;
  restorePromise = requestWechatIdentity().finally(() => {
    restorePromise = null;
  });
  return restorePromise;
}

function loginWithWechat({ phoneCode, displayName } = {}) {
  return requestWechatIdentity({ phoneCode, displayName });
}

function avatarContentType(fileBase64) {
  if (fileBase64.startsWith("iVBORw0KGgo")) return "image/png";
  if (fileBase64.startsWith("UklGR")) return "image/webp";
  return "image/jpeg";
}

function readFileBase64(filePath) {
  return new Promise((resolve, reject) => {
    wx.getFileSystemManager().readFile({
      filePath,
      encoding: "base64",
      success(result) {
        resolve(result.data);
      },
      fail(error) {
        reject(new ApiError(error?.errMsg || "头像读取失败。"));
      },
    });
  });
}

async function uploadWechatAvatar(filePath) {
  const fileBase64 = await readFileBase64(filePath);
  return rawRequest("/v1/auth/wechat-avatar", {
    method: "POST",
    data: {
      file_base64: fileBase64,
      content_type: avatarContentType(fileBase64),
    },
  });
}

function logoutLocal() {
  clearCachedRuntimeProfile();
  clearRuntimeProfileMemory();
  const app = currentApp();
  if (app) app.clearAuthenticatedIdentity();
}

function logoutCurrentDevice() {
  return rawRequest("/v1/auth/logout", { method: "POST" });
}

function logoutAllDevices() {
  return rawRequest("/v1/auth/logout-all", { method: "POST" });
}

async function requestAccountDeletion({ confirmation }) {
  const loginCode = await wechatLoginCode();
  return rawRequest("/v1/archive/deletion-requests", {
    method: "POST",
    data: {
      wechat_login_code: loginCode,
      confirmation,
    },
  });
}

function getProfile(userId) {
  return rawRequest(`/v1/memory/profile/${encodeURIComponent(userId)}`);
}

function getGuardianLinks() {
  return rawRequest("/v1/guardian/links").then(normalizeGuardianLinks);
}

function getGuardianSummary(minorUserId) {
  const cleanMinorUserId = encodeURIComponent(minorUserId);
  return rawRequest(`/v1/guardian/minors/${cleanMinorUserId}/summary`).then((payload) =>
    normalizeGuardianSummary(payload, minorUserId),
  );
}

function createGuardianLink({ minorUserId, relation = "parent", idempotencyKey }) {
  return rawRequest("/v1/guardian/links", {
    method: "POST",
    idempotencyKey,
    data: { minor_user_id: minorUserId, relation },
  });
}

function confirmGuardianLink({ linkId, bindingCode, birthYearBand }) {
  return rawRequest(`/v1/guardian/links/${encodeURIComponent(linkId)}/confirm`, {
    method: "POST",
    data: { binding_code: bindingCode, birth_year_band: birthYearBand },
  });
}

function getGuardianConsents(linkId) {
  return rawRequest(`/v1/guardian/links/${encodeURIComponent(linkId)}/consents`);
}

function grantGuardianConsent({ linkId, consentKind, policyVersion, idempotencyKey }) {
  return rawRequest(`/v1/guardian/links/${encodeURIComponent(linkId)}/consents`, {
    method: "POST",
    idempotencyKey,
    data: { consent_kind: consentKind, policy_version: policyVersion },
  });
}

function revokeGuardianConsent({ linkId, consentId, idempotencyKey }) {
  return rawRequest(
    `/v1/guardian/links/${encodeURIComponent(linkId)}/consents/${encodeURIComponent(consentId)}`,
    { method: "DELETE", idempotencyKey },
  );
}

function getGuardianNotifications() {
  return rawRequest("/v1/guardian/notifications");
}

function getTutorLessons(focus = "tutor_english") {
  return rawRequest(`/v1/tutor/lessons?focus=${encodeURIComponent(focus)}`);
}

function updateProfile(userId, profile) {
  const payload = {};
  for (const key of [
    "display_name",
    "bio",
    "auto_summary",
    "voice_reply",
    "gentle_reminders",
    "reject_non_owner_voice",
    "companion_id",
    "timezone",
  ]) {
    if (profile[key] !== undefined) payload[key] = profile[key];
  }
  return rawRequest(`/v1/memory/profile/${encodeURIComponent(userId)}`, {
    method: "PUT",
    data: payload,
  });
}

function getMemoryDays(userId, limit = 14) {
  return rawRequest(
    `/v1/memory/days?user_id=${encodeURIComponent(userId)}&limit=${encodeURIComponent(limit)}`,
  );
}

function summarizeDay(userId, date) {
  return rawRequest(`/v1/memory/days/${encodeURIComponent(date)}/summary`, {
    method: "POST",
    data: { user_id: userId },
  });
}

function getGrowthOverview() {
  return rawRequest("/v1/growth/overview");
}

function getPersonaStatus() {
  return rawRequest("/v1/persona/status");
}

function getDigitalSelfVersions() {
  return rawRequest("/v1/digital-self/versions");
}

function getRawVoiceConsent() {
  return rawRequest("/v1/archive/raw-voice-consent");
}

function grantRawVoiceConsent() {
  return rawRequest("/v1/archive/raw-voice-consent", {
    method: "POST",
    data: {
      policy_version: "raw-voice-archive-v1",
      retention_policy: "account_lifetime",
    },
  });
}

function revokeRawVoiceConsent() {
  return rawRequest("/v1/archive/raw-voice-consent", { method: "DELETE" });
}

function requiredOnboardingId(value, field) {
  if (typeof value !== "string" || !value || value.trim() !== value || value.length > 256) {
    throw new TypeError(`${field} 无效`);
  }
  return value;
}

function onboardingClientMetadata() {
  let appVersion = "unknown";
  let baseLibraryVersion = "unknown";
  try {
    const accountInfo = globalThis.wx?.getAccountInfoSync?.();
    appVersion = accountInfo?.miniProgram?.version || appVersion;
  } catch {
    // The server still receives a bounded, non-sensitive client marker.
  }
  try {
    const systemInfo = globalThis.wx?.getSystemInfoSync?.();
    baseLibraryVersion = systemInfo?.SDKVersion || baseLibraryVersion;
  } catch {
    // Optional on older runtimes.
  }
  return {
    platform: "wechat-miniprogram",
    app_version: appVersion,
    base_library_version: baseLibraryVersion,
  };
}

/*
 * Device Bootstrap API.  The QR string is passed as-is and is never written
 * to storage or included in a user-facing error.  Signature, certificate,
 * revocation, and session TTL decisions remain server authoritative.
 */
function introspectDeviceQr({ qrPayload, clientOnboardingId } = {}) {
  if (typeof qrPayload !== "string" || !qrPayload || qrPayload.length > 4096) {
    throw new TypeError("qr_payload 无效");
  }
  const clientId = requiredOnboardingId(clientOnboardingId, "client_onboarding_id");
  return rawRequest("/v1/device-bootstrap/introspect", {
    method: "POST",
    data: {
      qr_payload: qrPayload,
      client_onboarding_id: clientId,
      client: onboardingClientMetadata(),
    },
  }).then((payload) => normalizeIntrospectResponse(payload));
}

function getOnboardingSession(onboardingSessionId) {
  const sessionId = requiredOnboardingId(onboardingSessionId, "onboarding_session_id");
  return rawRequest(`/v1/device-bootstrap/${encodeURIComponent(sessionId)}`).then((payload) =>
    normalizeOnboardingSession(payload),
  );
}

function cancelOnboardingSession(onboardingSessionId) {
  const sessionId = requiredOnboardingId(onboardingSessionId, "onboarding_session_id");
  return rawRequest(`/v1/device-bootstrap/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
  }).then((payload) => normalizeOnboardingSession(payload));
}

function reserveDeviceClaim({
  onboardingSessionId,
  deviceId,
  idempotencyKey,
  expectedStateVersion,
} = {}) {
  const sessionId = requiredOnboardingId(onboardingSessionId, "onboarding_session_id");
  const targetDeviceId = requiredOnboardingId(deviceId, "device_id");
  const key = requiredOnboardingId(idempotencyKey, "idempotency_key");
  if (
    expectedStateVersion !== undefined &&
    (!Number.isInteger(expectedStateVersion) || expectedStateVersion < 1)
  ) {
    throw new TypeError("expected_state_version 无效");
  }
  return rawRequest("/v1/device-claims", {
    method: "POST",
    data: {
      onboarding_session_id: sessionId,
      device_id: targetDeviceId,
      idempotency_key: key,
      ...(expectedStateVersion === undefined ? {} : { expected_state_version: expectedStateVersion }),
    },
    idempotencyKey: key,
  }).then((payload) => normalizeClaimResponse(payload));
}

function getDeviceClaim(claimId) {
  const id = requiredOnboardingId(claimId, "claim_id");
  return rawRequest(`/v1/device-claims/${encodeURIComponent(id)}`).then((payload) =>
    normalizeClaimResponse(payload),
  );
}

function getActivationStatus(deviceId) {
  const id = requiredOnboardingId(deviceId, "device_id");
  return rawRequest(`/v1/device-activations/${encodeURIComponent(id)}`).then((payload) =>
    normalizeActivationResponse(payload),
  );
}

/*
 * 首次设备绑定（整改文档 §9.1）。请求体先经过 buildBindingRequest
 * fail-closed 校验：客户端不能提交 policy_version 或任何假授权字段。
 */
function createDeviceBinding(request, { idempotencyKey = "" } = {}) {
  const payload = buildBindingRequest(request);
  const previous = readBindingManifest();
  return rawRequest("/v1/device-bindings", {
    method: "POST",
    data: payload,
    idempotencyKey,
  }).then((manifest) => {
    saveBindingManifest(manifest);
    if (
      !previous ||
      previous.device_id !== manifest.device_id ||
      previous.binding_id !== manifest.binding_id ||
      previous.binding_version !== manifest.binding_version
    ) {
      // 重新绑定/版本变化：清空内存守卫，避免旧 epoch floor/代次残留。
      clearRuntimeProfileMemory();
    }
    return manifest;
  });
}

/*
 * 查询设备当前绑定（PR-04 后端提供；展示 declared_mode 与角色）。
 */
function getDeviceBinding(deviceId) {
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/binding`);
}

function getDeviceSettings(deviceId) {
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/settings`);
}

/*
 * 设备诊断（PR-18）。只展示服务端权威字段：绑定/设置快照、声学能力登记、
 * 允许的音频模式与 Runtime Profile 版本。端点不可用时由调用方 fail-closed，
 * 客户端不拼接或伪造连接质量字段。
 */
function getDeviceDiagnostics(deviceId) {
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/diagnostics/latest`);
}

function updateDeviceSettings(deviceId, changes, { expectedVersion } = {}) {
  if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
    return Promise.reject(new TypeError("设备设置无效"));
  }
  const allowed = new Set([
    "volume_limit",
    "screen_brightness",
    "night_mode",
    "do_not_disturb",
    "learning_mode",
    "audio_mode",
    "wake_mode",
    "allowed_barge_in",
  ]);
  const keys = Object.keys(changes);
  if (!keys.length || keys.some((key) => !allowed.has(key))) {
    return Promise.reject(new TypeError("设备设置包含不支持的字段"));
  }
  if (!Number.isInteger(expectedVersion) || expectedVersion < 0) {
    return Promise.reject(new TypeError("设备设置版本无效"));
  }
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/settings`, {
    method: "PATCH",
    data: {
      expected_settings_version: expectedVersion,
      changes,
    },
  });
}

/*
 * 解析当前会话主体（§9.2）。客户端不发送声纹原始数据，只传服务端
 * 已知的设备与会话标识。
 */
function resolveSessionSubject({
  deviceId,
  sessionId = null,
  clientClaimedPersonId = null,
  environment = {},
} = {}) {
  return rawRequest("/v1/sessions/resolve-subject", {
    method: "POST",
    data: {
      device_id: deviceId,
      session_id: sessionId,
      client_claimed_person_id: clientClaimedPersonId,
      environment,
    },
  }).then((payload) => normalizeSubjectResolution(payload));
}

/*
 * 获取 Runtime Profile（§9.3）。只读、带过期时间的服务端配置；
 * 客户端用它驱动敏感入口展示，而不是本地推断年龄。
 * 响应经过 canonical 严格校验（utils/device-binding.js）；晚到响应
 * （代次被更新的请求取代）、epoch 倒退、或 device/session/binding 与
 * 当前经过校验的 BindingManifest 不一致时返回 null，由调用方丢弃。
 */
function getRuntimeProfile(deviceId, { sessionId = null } = {}) {
  const manifest = readBindingManifest();
  if (!manifest || manifest.device_id !== deviceId) {
    return Promise.reject(new ApiError("还没有绑定设备，无法获取 Runtime Profile。", { status: 403 }));
  }
  const context = {
    deviceId: manifest.device_id,
    bindingId: manifest.binding_id,
    bindingVersion: manifest.binding_version,
    sessionId: sessionId || null,
  };
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const token = beginRuntimeProfileRequest(context);
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/runtime-profile${query}`).then(
    (payload) => {
      if (!isRuntimeProfileRequestCurrent(token)) return null;
      const profile = normalizeRuntimeProfile(payload);
      if (!isRuntimeProfileRequestCurrent(token)) return null;
      const accepted = acceptRuntimeProfile(profile, "refresh", context);
      if (accepted === null) return null;
      saveCachedRuntimeProfile(accepted);
      return accepted;
    },
  );
}

/*
 * 切换当前使用者（§9.5）。confirmation_method 只允许服务端声明的
 * 确认方式（如 app_confirm），客户端不能自行断言已通过声纹确认。
 * 同样受请求代次与 session_epoch 单调守卫：切换后只保存 epoch 提升的
 * 新 profile，晚到响应返回 null。
 */
function setActiveSubject(sessionId, { personId, confirmationMethod = "app_confirm" } = {}) {
  const manifest = readBindingManifest();
  if (!manifest) {
    return Promise.reject(new ApiError("还没有绑定设备，无法切换当前使用者。", { status: 403 }));
  }
  const context = {
    deviceId: manifest.device_id,
    bindingId: manifest.binding_id,
    bindingVersion: manifest.binding_version,
    sessionId: sessionId || null,
  };
  const token = beginRuntimeProfileRequest(context);
  return rawRequest(`/v1/sessions/${encodeURIComponent(sessionId)}/active-subject`, {
    method: "POST",
    data: {
      person_id: personId,
      confirmation_method: confirmationMethod,
    },
  }).then((payload) => {
    if (!isRuntimeProfileRequestCurrent(token)) return null;
    const profile = normalizeRuntimeProfile(payload);
    if (!isRuntimeProfileRequestCurrent(token)) return null;
    const accepted = acceptRuntimeProfile(profile, "switch", context);
    if (accepted === null) return null;
    saveCachedRuntimeProfile(accepted);
    return accepted;
  });
}

/*
 * 敏感入口的统一能力门禁（D-07）：只有当前设备取得有效 Runtime Profile
 * 且 capabilities 包含目标能力时才放行；Profile 不可用时返回可解释拒绝。
 * 所有敏感页面（数字分身/成长小结/原始语音/私人回顾）必须经此门禁。
 */
async function requireRuntimeCapability(capability, { sessionId = null } = {}) {
  const binding = readBindingManifest();
  if (!binding || typeof binding.device_id !== "string") {
    return { allowed: false, reason: "no_binding", profile: null };
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

module.exports = {
  ApiError,
  currentIdentity,
  currentAccessToken,
  hasAuthenticatedSession,
  currentAuthEpoch,
  isAuthEpochCurrent,
  requireAuthenticatedIdentity,
  restoreWechatIdentity,
  loginWithWechat,
  uploadWechatAvatar,
  logoutLocal,
  logoutCurrentDevice,
  logoutAllDevices,
  requestAccountDeletion,
  getProfile,
  getGuardianLinks,
  getGuardianSummary,
  createGuardianLink,
  confirmGuardianLink,
  getGuardianConsents,
  grantGuardianConsent,
  revokeGuardianConsent,
  getGuardianNotifications,
  getTutorLessons,
  updateProfile,
  getMemoryDays,
  summarizeDay,
  getGrowthOverview,
  getPersonaStatus,
  getDigitalSelfVersions,
  getRawVoiceConsent,
  grantRawVoiceConsent,
  revokeRawVoiceConsent,
  introspectDeviceQr,
  getOnboardingSession,
  cancelOnboardingSession,
  reserveDeviceClaim,
  getDeviceClaim,
  getActivationStatus,
  createDeviceBinding,
  getDeviceBinding,
  getDeviceSettings,
  getDeviceDiagnostics,
  updateDeviceSettings,
  resolveSessionSubject,
  getRuntimeProfile,
  setActiveSubject,
  requireRuntimeCapability,
  clearRuntimeProfileMemory,
};
