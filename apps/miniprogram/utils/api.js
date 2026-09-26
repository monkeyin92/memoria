const { CONTROL_API_BASE_URL } = require("../config");
const { normalizeGuardianLinks, normalizeGuardianSummary } = require("./guardian");
const {
  buildBindingRequest,
  normalizeRuntimeProfile,
  normalizeSubjectResolution,
  validateBindingManifest,
  readBindingManifest,
  saveBindingManifest,
  clearBindingManifest,
  saveCachedRuntimeProfile,
  clearCachedRuntimeProfile,
  canonicalWireJson,
  entryAllowed,
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
let deviceBindingContextRevision = 0;

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
  return { key, seq, contextRevision: deviceBindingContextRevision, authEpoch: currentAuthEpoch() };
}

function isRuntimeProfileRequestCurrent(token) {
  return (
    runtimeProfileSeqs.get(token.key) === token.seq &&
    token.contextRevision === deviceBindingContextRevision &&
    isAuthEpochCurrent(token.authEpoch)
  );
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

function clearDeviceBindingContext() {
  deviceBindingContextRevision += 1;
  clearBindingManifest();
  clearCachedRuntimeProfile();
  clearRuntimeProfileMemory();
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
                              : code === "guardian_binding_owner_required"
                                ? "只有监护绑定发起人可以修改这项资料或授权。"
                              : code === "subject_deletion_unavailable" ||
                                  code === "child_subject_deletion_unavailable"
                                ? "删除 TA 数据的功能还在完善中，暂时可以先选择保留数据。"
                                : code === "identity_authority_unavailable"
                                  ? "暂时无法确认身份资料，已按受限模式处理。"
                                  : code === "voice_clone_forbidden" || code === "voice_clone"
                            ? "当前账号未开启声音复刻。"
                          : code === "minor_forbidden"
                                ? "学生账号不开放这项能力。"
                                : code === "CLAIM_CONFLICT"
                                  ? "设备认领状态已变化，请刷新二维码后重试。"
                                  : code === "BINDING_CONFLICT" || code === "binding_conflict"
                                    ? "设备绑定状态发生冲突，请刷新二维码后重试。"
                                    : code === "DEVICE_ALREADY_BOUND"
                                      ? "这台机器人已经绑定，请先解除原绑定或更换设备。"
                                      : code === "STATE_VERSION_CONFLICT"
                                        ? "启用状态已更新，请返回设备页刷新后再提交。"
                                        : code === "INVALID_STATE_TRANSITION"
                                          ? "启用流程状态已变化，请返回设备页重新进入。"
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
    timeout = 60000,
  } = options;
  if (authenticated) requireAuthenticatedIdentity();
  const requestAuthEpoch = currentAuthEpoch();
  const requestAccessToken = authenticated ? currentAccessToken() : "";
  const headers = {
    Accept: "application/json",
    ...(data === undefined ? {} : { "content-type": "application/json" }),
    ...(authenticated ? { Authorization: `Bearer ${requestAccessToken}` } : {}),
    ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
  };
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${CONTROL_API_BASE_URL}${path}`,
      method,
      data,
      timeout,
      header: headers,
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data);
          return;
        }
        const error = errorFromResponse(response);
        if (authenticated && response.statusCode === 401 &&
            requestAuthEpoch === currentAuthEpoch() &&
            requestAccessToken === currentApp()?.globalData?.accessToken) {
          // 登录态失效：账号绑定、主体备注与 Runtime Profile 全部失效，
          // 防止切换微信账号后继续展示上一账号的设备。
          clearDeviceBindingContext();
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
  clearDeviceBindingContext();
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

/*
 * 主人声纹初始化只读取服务端状态。原始 PCM 和声纹模板不进入小程序；
 * 设备端登记完成后，页面重新拉取这里的权威结果。
 */
function getSpeakerEnrollmentStatus() {
  return rawRequest("/v1/speakers/status");
}

function grantVoiceCloneConsent() {
  return rawRequest("/v1/voices/consent", {
    method: "POST",
    data: { accepted: true, policy_version: "voice-clone-v1" },
  });
}

function listVoiceProfiles() {
  return rawRequest("/v1/voices/profiles");
}

function readyVoiceForDevice(profileId) {
  return rawRequest(
    `/v1/voices/profiles/${encodeURIComponent(profileId)}/ready-for-device`,
    { method: "POST" },
  );
}

function enrollVoiceClone({
  audioBase64,
  mediaType,
  durationMs,
  sampleRate = 16000,
  enrollmentKey,
  readyForDevice = true,
  customPersonaId = "",
}) {
  const data = {
    audio_base64: audioBase64,
    media_type: mediaType,
    duration_ms: durationMs,
    sample_rate: sampleRate,
    ready_for_device: Boolean(readyForDevice),
    ...(enrollmentKey ? { enrollment_key: enrollmentKey } : {}),
    // 只提交服务端已确认属于本账号的自定义人格 id；留空表示账号本人的声音。
    ...(customPersonaId ? { custom_persona_id: customPersonaId } : {}),
  };
  return rawRequest("/v1/voices/enrollments", {
    method: "POST",
    timeout: 120000,
    data,
  }).catch((error) => {
    if (!readyForDevice || error?.status !== 422) throw error;
    // 旧服务端连 ready_for_device 都不认：自定义人格字段同样不可用。
    const {
      ready_for_device: _ignored,
      custom_persona_id: _ignoredPersona,
      ...legacy
    } = data;
    return rawRequest("/v1/voices/enrollments", {
      method: "POST",
      timeout: 120000,
      data: legacy,
    });
  });
}

function createSpeakerEnrollmentIntent() {
  return rawRequest("/v1/speakers/enrollment-intents", {
    method: "POST",
    data: {
      consent_policy_version: "speaker-biometric-v1",
      consent_accepted: true,
    },
  });
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

/* 绑定人查看孩子/老人的人格：只返回固定风格标签，不含特征描述或原话。 */
function getSubjectPersonaStyle(subjectId) {
  const clean = encodeURIComponent(subjectId);
  return rawRequest(`/v1/persona/subjects/${clean}/style`).then((payload) => ({
    subjectId: typeof payload?.subject_id === "string" ? payload.subject_id : subjectId,
    versionNumber: Number.isInteger(payload?.version_number) ? payload.version_number : null,
    styleLabels: Array.isArray(payload?.style_labels)
      ? payload.style_labels.filter((label) => typeof label === "string" && label)
      : [],
  }));
}

function resetSubjectPersona(subjectId) {
  const clean = encodeURIComponent(subjectId);
  return rawRequest(`/v1/persona/subjects/${clean}/reset`, { method: "POST" });
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

/*
 * 无账号孩子的 person consent（P0-04）。主体由 parent_for_child 绑定创建，
 * 没有可确认的 guardian link。读写都按绑定 owner 走 person 端点，不按登录
 * 账号另查一份同意。幂等键与 link 级授权相同；过期/撤销由服务端 active 决定。
 */
function getPersonConsents(personId) {
  return rawRequest(`/v1/guardian/minors/${encodeURIComponent(personId)}/consents`);
}

function grantPersonConsent({ personId, consentKind, policyVersion, idempotencyKey }) {
  return rawRequest(`/v1/guardian/minors/${encodeURIComponent(personId)}/consents`, {
    method: "POST",
    idempotencyKey,
    data: { consent_kind: consentKind, policy_version: policyVersion },
  });
}

function revokePersonConsent({ personId, consentId, idempotencyKey }) {
  return rawRequest(
    `/v1/guardian/minors/${encodeURIComponent(personId)}/consents/${encodeURIComponent(consentId)}`,
    { method: "DELETE", idempotencyKey },
  );
}

/*
 * 无账号孩子的数据导出 / 删除。授权人是监护关系或当前 parent_for_child
 * 绑定 owner，由服务端决定。导出只含治理元数据（audience=guardian），不含
 * 对话原文；删除必须带服务端约定的确认文本，客户端不自行放宽。
 */
const GUARDIAN_MINOR_DELETE_CONFIRMATION = "永久删除孩子的全部数据";

function exportGuardianMinorData(personId) {
  return rawRequest(`/v1/guardian/minors/${encodeURIComponent(personId)}/export`, {
    method: "POST",
  });
}

function deleteGuardianMinorData(personId, { confirmation } = {}) {
  if (confirmation !== GUARDIAN_MINOR_DELETE_CONFIRMATION) {
    return Promise.reject(
      new TypeError(`请完整输入「${GUARDIAN_MINOR_DELETE_CONFIRMATION}」。`),
    );
  }
  return rawRequest(`/v1/guardian/minors/${encodeURIComponent(personId)}/delete`, {
    method: "POST",
    data: { confirmation },
  });
}

/*
 * 建后年龄资料申报。只接受 unknown / under_14 / 14_17；adult 与 verified
 * 不能由客户端申报。调用方是本人，或 ACTIVE parent_for_child 绑定 owner。
 * 成功只回写申报结果，不改会话、不重签 Runtime Profile。
 */
const DECLARABLE_AGE_BANDS = Object.freeze(["unknown", "under_14", "14_17"]);

function declareAgeEvidence(personId, ageBand) {
  if (!DECLARABLE_AGE_BANDS.includes(ageBand)) {
    return Promise.reject(new TypeError("年龄申报只接受未知、14 岁以下或 14 至 17 岁。"));
  }
  return rawRequest(`/v1/persons/${encodeURIComponent(personId)}/age-evidence`, {
    method: "PATCH",
    data: { age_band: ageBand },
  });
}

function getGuardianNotifications() {
  return rawRequest("/v1/guardian/notifications");
}

/* 危机提醒订阅（一次性订阅消息）。关闭时服务端不下发模板 ID。 */
function getGuardianPushConfig() {
  return rawRequest("/v1/guardian/push-config");
}

/* accept 必须附带新的 wx.login code，由服务端核对 openid 属于当前监护人。 */
function recordGuardianPushSubscription({ templateId, result, loginCode = "" }) {
  return rawRequest("/v1/guardian/push-subscriptions", {
    method: "POST",
    data: {
      template_id: templateId,
      result,
      ...(loginCode ? { login_code: loginCode } : {}),
    },
  });
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

/*
 * 私人回顾的权威投影（PR-19）。服务端按当前认证主体返回三个分区：
 * actual_heard / memory_candidates / confirmed_memories，客户端只消费
 * 窄字段，不透出完整 Archive payload。调用方必须先过 Runtime Profile 门禁。
 */
function getConversationReview() {
  return rawRequest("/v1/archive/conversation-review");
}

function getConversationSessions(limit = 10) {
  const parsedLimit = Number(limit);
  if (!Number.isInteger(parsedLimit) || parsedLimit < 1 || parsedLimit > 20) {
    return Promise.reject(new TypeError("会话列表参数无效"));
  }
  return rawRequest(`/v1/archive/conversation-sessions?limit=${parsedLimit}`);
}

function getConversationHistory(sessionId, turnLimit = 20) {
  if (
    typeof sessionId !== "string" ||
    !sessionId ||
    sessionId.trim() !== sessionId ||
    sessionId.length > 128
  ) {
    return Promise.reject(new TypeError("会话编号无效"));
  }
  const parsedLimit = Number(turnLimit);
  if (!Number.isInteger(parsedLimit) || parsedLimit < 1 || parsedLimit > 50) {
    return Promise.reject(new TypeError("对话轮数参数无效"));
  }
  return rawRequest(
    `/v1/archive/conversation-history?session_id=${encodeURIComponent(sessionId)}&turn_limit=${parsedLimit}`,
  );
}

/*
 * 候选记忆确认（PR-19）。动作只允许服务端定义的 confirm；客户端确认
 * 成功后必须重新拉取权威投影，绝不本地把 candidate 直接升级为已确认。
 */
function reviewMemoryClaim(claimId, action = "confirm") {
  if (
    typeof claimId !== "string" ||
    !claimId ||
    claimId.trim() !== claimId ||
    claimId.length > 256
  ) {
    return Promise.reject(new TypeError("记忆确认参数无效"));
  }
  if (action !== "confirm") {
    return Promise.reject(new TypeError("不支持的记忆确认动作"));
  }
  return rawRequest(`/v1/archive/memories/${encodeURIComponent(claimId)}/review`, {
    method: "POST",
    data: { action },
  });
}

function getDeliveredCapabilities() {
  return rawRequest("/v1/account/delivered-capabilities");
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
  return rawRequest("/v1/device-bindings", {
    method: "POST",
    data: payload,
    idempotencyKey,
  }).then((manifest) => selectDeviceBinding(manifest));
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function invalidBindingDiscovery(detail) {
  return new ApiError(`设备同步结果无效：${detail}`, {
    code: "invalid_device_bindings",
  });
}

function normalizeDeviceBindings(payload) {
  if (!isPlainObject(payload)) {
    throw invalidBindingDiscovery("响应不是对象");
  }
  if (!Array.isArray(payload.bindings)) {
    throw invalidBindingDiscovery("bindings 必须是数组");
  }
  const bindingIds = new Set();
  const deviceIds = new Set();
  const bindings = payload.bindings.map((item) => {
    const result = validateBindingManifest(item);
    if (!result.valid) {
      throw invalidBindingDiscovery(result.reasons[0] || "绑定清单不完整");
    }
    const manifest = result.manifest;
    if (manifest.status !== "active") {
      throw invalidBindingDiscovery("服务端返回了非生效绑定");
    }
    if (bindingIds.has(manifest.binding_id)) {
      throw invalidBindingDiscovery("存在重复绑定编号");
    }
    if (deviceIds.has(manifest.device_id)) {
      throw invalidBindingDiscovery("同一设备存在多个生效绑定");
    }
    bindingIds.add(manifest.binding_id);
    deviceIds.add(manifest.device_id);
    return manifest;
  });
  return bindings;
}

/*
 * 账号级绑定发现。手机、电脑微信与开发者工具的本地存储彼此隔离，
 * 所以登录后必须从服务端恢复账号可见的 active BindingManifest，不能把
 * “本机无缓存”直接解释成“账号没有设备”。
 */
function listDeviceBindings() {
  return rawRequest("/v1/device-bindings").then(normalizeDeviceBindings);
}

function selectDeviceBinding(manifest) {
  const validated = validateBindingManifest(manifest);
  if (!validated.valid || validated.manifest.status !== "active") {
    throw invalidBindingDiscovery(validated.reasons[0] || "绑定已失效");
  }
  const next = validated.manifest;
  const previous = readBindingManifest();
  // 先推进上下文代次，再写入本地。任何更早发起的账号级发现请求即使后到，
  // 也只能读取当前选择，不能把旧选择重新写回来。
  deviceBindingContextRevision += 1;
  saveBindingManifest(next);
  if (
    !previous ||
    previous.device_id !== next.device_id ||
    previous.binding_id !== next.binding_id ||
    previous.binding_version !== next.binding_version
  ) {
    // 新客户端恢复、换设备或绑定版本变化时，旧请求代次与 epoch floor
    // 都不能继续约束新的 Runtime Profile。
    clearRuntimeProfileMemory();
  }
  return next;
}

function bindingStateAfterContextChange(bindings = []) {
  const current = readBindingManifest();
  if (current) {
    return {
      status: "ready",
      binding: current,
      bindings,
      contextChanged: true,
    };
  }
  return {
    status: "empty",
    binding: null,
    bindings: [],
    contextChanged: true,
  };
}

async function syncDeviceBindings() {
  const startedAtRevision = deviceBindingContextRevision;
  const startedAtAuthEpoch = currentAuthEpoch();
  try {
    const bindings = await listDeviceBindings();
    if (
      startedAtRevision !== deviceBindingContextRevision ||
      startedAtAuthEpoch !== currentAuthEpoch() ||
      !hasAuthenticatedSession()
    ) {
      return bindingStateAfterContextChange(bindings);
    }
    if (bindings.length === 0) {
      // 只有服务端明确确认账号没有 active binding 时，才清理本地上下文。
      clearDeviceBindingContext();
      return { status: "empty", binding: null, bindings: [] };
    }

    let selected = null;
    const cached = readBindingManifest();
    if (cached) {
      selected =
        bindings.find((item) => item.binding_id === cached.binding_id) ||
        bindings.find((item) => item.device_id === cached.device_id) ||
        null;
    }
    if (!selected && bindings.length === 1) selected = bindings[0];

    if (selected) {
      return {
        status: "ready",
        binding: selectDeviceBinding(selected),
        bindings,
      };
    }
    // 当前账号仍有设备，但此前选中的绑定已不可见；不要让其他页面复用它。
    if (cached) clearDeviceBindingContext();
    return { status: "choose", binding: null, bindings };
  } catch (error) {
    // 另一页面已选择设备或账号已登出时，只读取当前上下文；绝不能用本次
    // 请求开始时的旧快照回填。401 已由 rawRequest 完成全量清理。
    if (error?.status === 401) {
      return { status: "error", binding: null, bindings: [], error };
    }
    if (
      startedAtRevision !== deviceBindingContextRevision ||
      startedAtAuthEpoch !== currentAuthEpoch()
    ) {
      return bindingStateAfterContextChange();
    }
    const current = readBindingManifest();
    if (current) {
      return { status: "cached", binding: current, bindings: [], error };
    }
    return { status: "error", binding: null, bindings: [], error };
  }
}

/*
 * 查询设备当前绑定（PR-04 后端提供；展示 declared_mode 与角色）。
 */
function getDeviceBinding(deviceId) {
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/binding`);
}

/*
 * 解除绑定。服务端先停止该使用人的记忆，再按 purge_subject_data 决定是否
 * 同时删除 TA 的记忆与对话数据；false 表示保留，重新绑定后可恢复。成功后
 * 清理本地绑定上下文，由调用方重新同步账号设备。
 */
function unbindDevice(deviceId, { purgeSubjectData } = {}) {
  if (typeof purgeSubjectData !== "boolean") {
    return Promise.reject(new TypeError("请先选择是否删除使用人的数据。"));
  }
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/binding/unbind`, {
    method: "POST",
    data: { reason: "unbind", purge_subject_data: purgeSubjectData },
  }).then((result) => {
    const current = readBindingManifest();
    if (!current || current.device_id === deviceId) clearDeviceBindingContext();
    return result;
  });
}

function getDeviceSettings(deviceId) {
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/settings`);
}

function getWakeWordCatalog() {
  return rawRequest("/v1/devices/wake-word-catalog");
}

function validateWakeWord(payload) {
  return rawRequest("/v1/devices/wake-word/validate", {
    method: "POST",
    data: payload,
  });
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
    "wake_word_id",
    "wake_word_pinyin",
    "wake_word_display",
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
 * 自定义人格（P1-03/P1-04）：账号级不可变 cu_ 人格，创建即冻结 v1。
 * `/v1/personas/structuring` 是纯函数（不落库），离线/未接入时返回 503，
 * 客户端回落到按同一受控字段手填后再 `POST /v1/personas`。
 */
function structureCustomPersona(freeText) {
  return rawRequest("/v1/personas/structuring", {
    method: "POST",
    data: { free_text: freeText },
  });
}

function createCustomPersona({ displayName, structured, fallbackDesignedVoice = "starlight" }) {
  return rawRequest("/v1/personas", {
    method: "POST",
    data: {
      display_name: displayName,
      structured,
      fallback_designed_voice: fallbackDesignedVoice,
    },
  });
}

function deleteCustomPersona(personaId, { confirm = false } = {}) {
  const query = confirm ? "?confirm=true" : "";
  return rawRequest(`/v1/personas/${encodeURIComponent(personaId)}${query}`, {
    method: "DELETE",
  });
}

/*
 * 账号的人格目录（P1-03/P1-04）：只读的内置目录 + 账号自己的 cu_ 自建人格。
 * 只用于展示与选择；提交分配时只给 persona_id，版本由服务端决定。
 */
function listPersonas() {
  return rawRequest("/v1/personas");
}

/*
 * 按使用人分配人格（P1-03）。写入口只有服务端，客户端只转发并回读，
 * 不推断可用人格、不本地缓存分配结果；写入后服务端在下一次设备同步
 * 就按新人格签发 Runtime Profile。
 */
function listPersonaAssignments(deviceId) {
  const manifest = readBindingManifest();
  if (!manifest || manifest.device_id !== deviceId) {
    return Promise.reject(new ApiError("还没有绑定设备，无法读取人格分配。", { status: 403 }));
  }
  return rawRequest(`/v1/devices/${encodeURIComponent(deviceId)}/persona-assignments`);
}

function setPersonaAssignment(deviceId, personId, personaSelection) {
  const manifest = readBindingManifest();
  if (!manifest || manifest.device_id !== deviceId) {
    return Promise.reject(new ApiError("还没有绑定设备，无法分配人格。", { status: 403 }));
  }
  return rawRequest(
    `/v1/devices/${encodeURIComponent(deviceId)}/persona-assignments/${encodeURIComponent(personId)}`,
    { method: "PUT", data: { persona_selection: personaSelection } },
  );
}

function clearPersonaAssignment(deviceId, personId) {
  const manifest = readBindingManifest();
  if (!manifest || manifest.device_id !== deviceId) {
    return Promise.reject(new ApiError("还没有绑定设备，无法取消人格分配。", { status: 403 }));
  }
  return rawRequest(
    `/v1/devices/${encodeURIComponent(deviceId)}/persona-assignments/${encodeURIComponent(personId)}`,
    { method: "DELETE" },
  );
}

/*
 * 敏感入口的统一能力门禁（D-07）：只有当前设备取得有效 Runtime Profile
 * 且 capabilities 包含目标能力时才放行；Profile 不可用时返回可解释拒绝。
 * 所有敏感页面（数字分身/成长小结/原始语音/私人回顾）必须经此门禁。
 */
async function requireRuntimeCapability(capability, { sessionId = null } = {}) {
  const authEpoch = currentAuthEpoch();
  let binding = readBindingManifest();
  if (!binding || typeof binding.device_id !== "string") {
    if (!hasAuthenticatedSession()) {
      return { allowed: false, reason: "unauthenticated", profile: null };
    }
    // 回顾、隐私等页面也可以成为登录后的第一站，不能依赖首页先恢复缓存。
    // 账号绑定发现只恢复设备上下文，绝不代替下方的服务端能力校验。
    const discovered = await syncDeviceBindings();
    if (!isAuthEpochCurrent(authEpoch) || !hasAuthenticatedSession()) {
      return { allowed: false, reason: "superseded", profile: null };
    }
    if (discovered.status === "choose") {
      return { allowed: false, reason: "device_selection_required", profile: null };
    }
    if (discovered.contextChanged && !discovered.binding) {
      return { allowed: false, reason: "superseded", profile: null };
    }
    if (discovered.status === "empty") {
      return { allowed: false, reason: "no_binding", profile: null };
    }
    if (!discovered.binding) {
      return {
        allowed: false,
        reason: "binding_sync_failed",
        profile: null,
        error: discovered.error,
      };
    }
    binding = discovered.binding;
  }
  try {
    const profile = await getRuntimeProfile(binding.device_id, { sessionId });
    if (!isAuthEpochCurrent(authEpoch) || profile === null) {
      return { allowed: false, reason: "superseded", profile: null };
    }
    if (profile.valid !== true) {
      return { allowed: false, reason: "invalid_profile", profile };
    }
    // 动作时决策的能力不会出现在 profile 里，由动作接口的服务端结果决定。
    if (!entryAllowed(profile, capability)) {
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
  getSpeakerEnrollmentStatus,
  createSpeakerEnrollmentIntent,
  grantVoiceCloneConsent,
  listVoiceProfiles,
  readyVoiceForDevice,
  enrollVoiceClone,
  getGuardianLinks,
  getGuardianSummary,
  createGuardianLink,
  confirmGuardianLink,
  getGuardianConsents,
  grantGuardianConsent,
  revokeGuardianConsent,
  readBindingManifest,
  getPersonConsents,
  grantPersonConsent,
  revokePersonConsent,
  GUARDIAN_MINOR_DELETE_CONFIRMATION,
  exportGuardianMinorData,
  deleteGuardianMinorData,
  declareAgeEvidence,
  getGuardianNotifications,
  getGuardianPushConfig,
  recordGuardianPushSubscription,
  wechatLoginCode,
  getTutorLessons,
  updateProfile,
  getMemoryDays,
  summarizeDay,
  getConversationReview,
  getConversationSessions,
  getConversationHistory,
  reviewMemoryClaim,
  getDeliveredCapabilities,
  getGrowthOverview,
  getPersonaStatus,
  getSubjectPersonaStyle,
  resetSubjectPersona,
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
  listDeviceBindings,
  syncDeviceBindings,
  selectDeviceBinding,
  getDeviceBinding,
  unbindDevice,
  getDeviceSettings,
  getWakeWordCatalog,
  validateWakeWord,
  getDeviceDiagnostics,
  updateDeviceSettings,
  resolveSessionSubject,
  getRuntimeProfile,
  setActiveSubject,
  listPersonaAssignments,
  setPersonaAssignment,
  clearPersonaAssignment,
  listPersonas,
  structureCustomPersona,
  createCustomPersona,
  deleteCustomPersona,
  requireRuntimeCapability,
  clearRuntimeProfileMemory,
};
