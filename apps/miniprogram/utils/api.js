const { CONTROL_API_BASE_URL } = require("../config");
const { normalizeGuardianLinks, normalizeGuardianSummary } = require("./guardian");

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
              : code === "miniprogram_media_gateway_unavailable"
                ? "小程序语音入口暂未部署，请稍后再试。"
                : code === "miniprogram_requires_cascade"
                  ? "小程序当前只支持级联语音服务。"
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

function createMiniProgramSession({
  userId,
  learningTaskId = null,
  interactionMode = "companion",
  sessionFocus = "chat",
}) {
  return rawRequest("/v1/sessions", {
    method: "POST",
    data: {
      user_id: userId,
      voice_backend: "cascade",
      interaction_mode: interactionMode,
      session_focus: interactionMode === "companion" ? sessionFocus : "chat",
      learning_task_id: interactionMode === "companion" ? learningTaskId : null,
      locale: "zh-CN",
      client: {
        platform: "miniprogram",
        timezone: "Asia/Shanghai",
      },
    },
  }).then((session) => {
    if (
      !session ||
      session.voice_backend !== "cascade" ||
      !session.media_gateway ||
      typeof session.media_gateway.websocket_url !== "string" ||
      typeof session.media_gateway.ticket !== "string"
    ) {
      throw new ApiError("服务端没有返回可用的小程序语音会话。");
    }
    return session;
  });
}

function refreshMiniProgramGatewayTicket(sessionId) {
  return rawRequest(
    `/v1/sessions/${encodeURIComponent(sessionId)}/mini-program/gateway-ticket`,
    { method: "POST" },
  );
}

function stopResponse(sessionId) {
  return rawRequest(`/v1/sessions/${encodeURIComponent(sessionId)}/stop-response`, {
    method: "POST",
    data: { reason: "user_button" },
  });
}

function notifyRtcRecovered(sessionId) {
  return rawRequest(`/v1/sessions/${encodeURIComponent(sessionId)}/rtc-recovered`, {
    method: "POST",
  });
}

function enrollSpeakerProfiles(samples) {
  return rawRequest("/v1/speakers/enrollments", {
    method: "POST",
    data: {
      consent_policy_version: "speaker-biometric-v1",
      consent_accepted: true,
      samples,
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
  createMiniProgramSession,
  refreshMiniProgramGatewayTicket,
  stopResponse,
  notifyRtcRecovered,
  enrollSpeakerProfiles,
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
};
