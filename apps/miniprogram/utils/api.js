const { CONTROL_API_BASE_URL } = require("../config");

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
  return currentApp()?.globalData?.accessToken || "";
}

function setAuthenticatedIdentity(response) {
  const app = currentApp();
  if (!app) throw new Error("小程序尚未初始化");
  return app.setAuthenticatedIdentity(response);
}

function requireAuthenticatedIdentity() {
  const identity = currentIdentity();
  if (!identity || !currentAccessToken()) {
    throw new ApiError("请先登录或开始匿名体验。", { status: 401 });
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
      : code === "miniprogram_media_gateway_unavailable"
        ? "小程序语音入口暂未部署，请稍后再试。"
        : code === "miniprogram_requires_cascade"
          ? "小程序当前只支持级联语音服务。"
          : `请求未完成（${response.statusCode || 0}）`;
  return new ApiError(message, { status: response.statusCode || 0, code });
}

function rawRequest(path, options = {}) {
  const {
    method = "GET",
    data,
    authenticated = true,
  } = options;
  if (authenticated) requireAuthenticatedIdentity();
  const headers = {
    Accept: "application/json",
    ...(data === undefined ? {} : { "content-type": "application/json" }),
    ...(authenticated ? { Authorization: `Bearer ${currentAccessToken()}` } : {}),
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
        reject(errorFromResponse(response));
      },
      fail(error) {
        reject(new ApiError(error?.errMsg || "网络连接失败，请稍后重试。"));
      },
    });
  });
}

async function issueAnonymousIdentity() {
  const response = await rawRequest("/v1/auth/anonymous", { method: "POST", authenticated: false });
  return setAuthenticatedIdentity(response);
}

async function registerAccount(username, password) {
  const response = await rawRequest("/v1/auth/register", {
    method: "POST",
    data: { username, password },
    authenticated: false,
  });
  return setAuthenticatedIdentity(response);
}

async function loginAccount(username, password) {
  const response = await rawRequest("/v1/auth/login", {
    method: "POST",
    data: { username, password },
    authenticated: false,
  });
  return setAuthenticatedIdentity(response);
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

function requestAccountDeletion({ password, confirmation }) {
  return rawRequest("/v1/archive/deletion-requests", {
    method: "POST",
    data: { password, confirmation },
  });
}

function createMiniProgramSession({ userId, learningTaskId = null, interactionMode = "companion" }) {
  return rawRequest("/v1/sessions", {
    method: "POST",
    data: {
      user_id: userId,
      voice_backend: "cascade",
      interaction_mode: interactionMode,
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

function getProfile(userId) {
  return rawRequest(`/v1/memory/profile/${encodeURIComponent(userId)}`);
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
  requireAuthenticatedIdentity,
  issueAnonymousIdentity,
  registerAccount,
  loginAccount,
  logoutLocal,
  logoutCurrentDevice,
  logoutAllDevices,
  requestAccountDeletion,
  createMiniProgramSession,
  refreshMiniProgramGatewayTicket,
  stopResponse,
  notifyRtcRecovered,
  getProfile,
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
