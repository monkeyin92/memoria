const baseUrl = (import.meta.env.VITE_CONTROL_API_URL || "/memoria-api").replace(
  /\/$/,
  "",
);

const identityStorageKey = "memoria:identity";
const legacyIdentityStorageKey = "memoria:anonymous-identity";
let activeIdentity = null;
let identityPromise = null;
let refreshPromise = null;

const apiErrorMessages = {
  account_deletion_in_progress: "账号正在删除，当前操作已停止。",
  account_not_registered: "请先完成账号注册后再管理数字分身版本。",
  empty_source: "还没有已确认的记忆或人格材料，暂时无法构建数字分身草稿。",
  invalid_transition: "当前状态不允许此操作，或状态已发生变化，请刷新后重试。",
  manifest_conflict: "版本摘要或内容校验失败，请刷新后重试。",
  manifest_integrity: "版本内容完整性校验失败，请刷新后重试。",
  source_snapshot_conflict: "确认材料在构建期间发生变化，请重新生成草稿。",
  step_up_failed: "账号密码不正确，操作没有执行。",
  version_not_found: "数字分身版本不存在或不属于当前账号。",
  revision_conflict: "成长任务状态已变化，请刷新后重试。",
  task_not_found: "成长任务不存在或不属于当前账号。",
  feedback_target_mismatch: "这条来源已无法确认归属，请刷新后重试。",
  ineligible_owner_source: "这条材料暂时不能用于成长任务，请稍后重试。",
  self_model_feedback_conflict: "这条认知材料已经变化，请刷新后重试。",
  self_model_projection_conflict: "回答已保存，但认知候选暂时未同步，请重试。",
  self_model_item_not_found: "这条认知材料不存在或不属于当前账号。",
  self_model_version_conflict: "认知材料已发生变化，请刷新后重试。",
  idempotency_conflict: "同一操作标识已用于不同内容，请刷新后重试。",
  untrusted_owner_source: "所选来源不是可用于数字分身的本人证据。",
  relationship_reference_invalid: "人物或关系来源已经变化，请刷新后重试。",
  invalid_self_model_input: "认知材料内容不完整，请检查后重试。",
  preview_prerequisite_missing: "数字分身预览的安全条件尚未满足。",
  preview_grant_conflict: "预览授权已经变化，请重新确认后再试。",
  preview_grant_not_found: "预览授权不存在或不属于当前账号。",
  preview_grant_unavailable: "预览授权已使用、撤销或过期，请重新确认。",
  preview_version_unavailable: "该数字分身版本当前不可用于预览。",
  preview_version_stale: "该版本已收到新的纠正，需要重新构建并审核。",
  self_preview_requires_controlled_backend: "数字分身预览只允许使用受控语音链路。",
  preview_response_not_found: "这轮回答还没有可核验的已听证据。",
  preview_response_fence_mismatch: "回答来源已变化，请刷新后重试。",
  preview_source_invalid: "回答来源校验失败，已停止展示。",
  preview_feedback_scope_mismatch: "这条反馈不属于当前预览会话。",
  preview_feedback_source_mismatch: "反馈来源已变化，请刷新后重试。",
  preview_feedback_conflict: "这条反馈已记录或内容发生冲突。",
  fidelity_conflict: "忠实度评测状态已变化，请刷新后重试。",
  fidelity_readiness_failed: "当前评测尚未满足完成条件。",
  fidelity_evaluation_not_found: "忠实度评测不存在或不属于当前账号。",
  fidelity_trial_not_found: "这道保留题不存在或已变化。",
  fidelity_approval_required: "请先完成忠实度评测并明确批准这个版本。",
};

function identitySnapshot(identity) {
  if (!identity || typeof identity.user_id !== "string" || !identity.user_id) {
    return null;
  }
  return {
    user_id: identity.user_id,
    username: identity.username || null,
    account_type: identity.account_type || "registered",
  };
}

function readStoredIdentity() {
  try {
    const read = (key) => {
      try {
        return JSON.parse(window.localStorage.getItem(key) || "null");
      } catch {
        return null;
      }
    };
    const current = read(identityStorageKey);
    const legacy = read(legacyIdentityStorageKey);
    const currentSnapshot = identitySnapshot(current);
    const legacySnapshot = identitySnapshot(legacy);
    if (currentSnapshot && !current?.access_token) {
      window.localStorage.removeItem(legacyIdentityStorageKey);
      return currentSnapshot;
    }
    const value = currentSnapshot && current?.access_token
      ? current
      : legacySnapshot && legacy?.access_token
        ? legacy
        : current || legacy;
    const snapshot = identitySnapshot(value);
    if (!snapshot) return null;
    const legacyAccessToken = typeof value?.access_token === "string"
      ? value.access_token
      : null;
    return {
      ...snapshot,
      ...(legacyAccessToken
        ? { legacy_access_token: legacyAccessToken }
        : {}),
    };
  } catch {
    // Invalid local identity data is ignored; the account gate will recover it.
  }
  return null;
}

async function upgradeLegacyAccess(token) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      return await performRequest(
        "/v1/auth/upgrade",
        { method: "POST", headers: { Authorization: `Bearer ${token}` } },
        { authenticated: false },
      );
    } catch (error) {
      if (attempt || error?.status) throw error;
    }
  }
  throw new Error("legacy upgrade retry exhausted");
}

async function performRequest(
  path,
  options = {},
  { authenticated = true, responseType = "json" } = {},
) {
  if (authenticated && !activeIdentity?.access_token) {
    throw new Error("账号身份尚未就绪");
  }
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(authenticated
        ? { Authorization: `Bearer ${activeIdentity.access_token}` }
        : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    let message = detail;
    let errorCode = null;
    try {
      const parsed = JSON.parse(detail);
      if (typeof parsed?.detail === "string") message = parsed.detail;
      if (typeof parsed?.detail?.code === "string") {
        errorCode = parsed.detail.code;
        message = apiErrorMessages[errorCode] || "请求未完成，请刷新后重试。";
      }
    } catch {
      // Non-JSON upstream failures keep their safe response text.
    }
    const error = new Error(message || `请求失败（${response.status}）`);
    error.status = response.status;
    error.code = errorCode;
    error.retryAfter = response.headers?.get?.("Retry-After") || null;
    throw error;
  }

  if (response.status === 204) return null;
  if (responseType === "blob") return response.blob();
  if (responseType === "text") return response.text();
  return response.json();
}

function persistSnapshot(identity) {
  const snapshot = identitySnapshot(identity);
  if (!snapshot) throw new Error("账号身份响应无效");
  window.localStorage.setItem(identityStorageKey, JSON.stringify(snapshot));
  window.localStorage.removeItem(legacyIdentityStorageKey);
  return snapshot;
}

function acceptAuthIdentity(identity) {
  const snapshot = identitySnapshot(identity);
  if (!snapshot || typeof identity.access_token !== "string" || !identity.access_token) {
    throw new Error("账号身份响应无效");
  }
  activeIdentity = {
    ...snapshot,
    access_token: identity.access_token,
  };
  return persistSnapshot(snapshot);
}

function clearActiveIdentity({ removeSnapshot = true } = {}) {
  activeIdentity = null;
  if (removeSnapshot) {
    window.localStorage.removeItem(identityStorageKey);
    window.localStorage.removeItem(legacyIdentityStorageKey);
  }
}

function refreshRetryDelayMs(error) {
  const seconds = Number.parseFloat(error?.retryAfter);
  if (!Number.isFinite(seconds) || seconds < 0) return 250;
  return Math.min(seconds * 1_000, 5_000);
}

async function performRefreshWithRaceRetry() {
  try {
    return await performRequest(
      "/v1/auth/refresh",
      { method: "POST" },
      { authenticated: false },
    );
  } catch (error) {
    if (error?.status !== 409) throw error;
    await new Promise((resolve) => setTimeout(resolve, refreshRetryDelayMs(error)));
    return performRequest(
      "/v1/auth/refresh",
      { method: "POST" },
      { authenticated: false },
    );
  }
}

function refreshAccess() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = performRefreshWithRaceRetry()
    .then(acceptAuthIdentity)
    .catch((error) => {
      clearActiveIdentity({ removeSnapshot: [401, 403].includes(error?.status) });
      throw error;
    })
    .finally(() => {
      refreshPromise = null;
    });
  return refreshPromise;
}

async function request(
  path,
  options = {},
  { authenticated = true, responseType = "json" } = {},
) {
  try {
    return await performRequest(path, options, { authenticated, responseType });
  } catch (error) {
    if (!authenticated || error?.status !== 401) throw error;
    await refreshAccess();
    return performRequest(path, options, { authenticated, responseType });
  }
}

export function bootstrapIdentity() {
  if (activeIdentity) return Promise.resolve(identitySnapshot(activeIdentity));
  if (identityPromise) return identityPromise;

  const stored = readStoredIdentity();
  identityPromise = (async () => {
    try {
      if (stored?.legacy_access_token) {
        const upgraded = await upgradeLegacyAccess(stored.legacy_access_token);
        return acceptAuthIdentity(upgraded);
      }
      return await refreshAccess();
    } catch (error) {
      if ([401, 403].includes(error?.status)) {
        clearActiveIdentity();
        return null;
      }
      clearActiveIdentity({ removeSnapshot: false });
      throw error;
    }
  })().finally(() => {
    identityPromise = null;
  });
  return identityPromise;
}

export async function registerAccount(username, password) {
  const identity = await request(
    "/v1/auth/register",
    {
      method: "POST",
      body: JSON.stringify({ username, password }),
    },
    { authenticated: Boolean(activeIdentity?.access_token) },
  );
  if (!identity?.user_id || !identity?.access_token) {
    throw new Error("账号注册响应无效");
  }
  return acceptAuthIdentity(identity);
}

export async function loginAccount(username, password) {
  const identity = await request(
    "/v1/auth/login",
    {
      method: "POST",
      body: JSON.stringify({ username, password }),
    },
    { authenticated: false },
  );
  if (!identity?.user_id || !identity?.access_token) {
    throw new Error("账号登录响应无效");
  }
  return acceptAuthIdentity(identity);
}

export async function createAnonymousIdentity() {
  const identity = await request(
    "/v1/auth/anonymous",
    { method: "POST" },
    { authenticated: false },
  );
  return acceptAuthIdentity(identity);
}

async function logout(path) {
  await request(path, { method: "POST" });
  clearActiveIdentity();
}

export function logoutCurrentDevice() {
  return logout("/v1/auth/logout");
}

export function logoutAllDevices() {
  return logout("/v1/auth/logout-all");
}

function requireCompanionInteraction(session) {
  const interaction = session?.interaction;
  const voiceProfileId = interaction?.voice_profile_id ?? null;
  const voiceProfileVersion = interaction?.voice_profile_version ?? null;
  const voiceProvider = interaction?.voice_provider ?? null;
  const voiceModel = interaction?.voice_model ?? null;
  const voiceResourceId = interaction?.voice_resource_id ?? null;
  const voiceProviderExpiresAt = interaction?.voice_provider_expires_at ?? null;
  const voiceSpeakerSha256 = interaction?.voice_speaker_sha256 ?? null;
  if (
    interaction?.interaction_mode !== "companion" ||
    typeof interaction.mode_policy_version !== "string" ||
    !interaction.mode_policy_version ||
    typeof interaction.companion_style_id !== "string" ||
    !interaction.companion_style_id ||
    typeof interaction.companion_style_version !== "string" ||
    !interaction.companion_style_version ||
    interaction.digital_self_version_id !== null ||
    (interaction.manifest_sha256 ?? null) !== null ||
    (interaction.preview_grant_id ?? null) !== null ||
    (interaction.perspective ?? null) !== null ||
    interaction.relationship_profile_id !== null ||
    interaction.legacy_grant_id !== null ||
    voiceProfileId !== null ||
    voiceProfileVersion !== null ||
    voiceProvider !== null ||
    voiceModel !== null ||
    voiceResourceId !== null ||
    voiceProviderExpiresAt !== null ||
    voiceSpeakerSha256 !== null ||
    (interaction.fallback_voice_profile_id ?? null) !== null ||
    (interaction.fallback_voice_provider ?? null) !== null ||
    (interaction.fallback_voice_model ?? null) !== null ||
    (interaction.fallback_voice_resource_id ?? null) !== null
  ) {
    throw new Error("服务端没有返回可验证的陪伴模式，会话已停止");
  }
  return session;
}

const selfPreviewPerspectives = new Set(["owner", "child", "friend"]);

function requireSelfPreviewInteraction(session) {
  const interaction = session?.interaction;
  const capabilities = interaction?.capabilities;
  const voiceProfileId = interaction?.voice_profile_id ?? null;
  const voiceProfileVersion = interaction?.voice_profile_version ?? null;
  const voiceProvider = interaction?.voice_provider ?? null;
  const voiceModel = interaction?.voice_model ?? null;
  const voiceResourceId = interaction?.voice_resource_id ?? null;
  const voiceProviderExpiresAt = interaction?.voice_provider_expires_at ?? null;
  const voiceSpeakerSha256 = interaction?.voice_speaker_sha256 ?? null;
  if (
    session?.voice_backend !== "cascade" ||
    interaction?.interaction_mode !== "self_preview" ||
    typeof interaction.mode_policy_version !== "string" ||
    !interaction.mode_policy_version ||
    typeof interaction.digital_self_version_id !== "string" ||
    !interaction.digital_self_version_id ||
    typeof interaction.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(interaction.manifest_sha256) ||
    typeof interaction.preview_grant_id !== "string" ||
    !interaction.preview_grant_id ||
    !selfPreviewPerspectives.has(interaction.perspective) ||
    interaction.simulated_output !== true ||
    interaction.history_eligible !== false ||
    interaction.owner_projection_eligible !== false ||
    interaction.companion_style_id !== null ||
    interaction.companion_style_version !== null ||
    interaction.relationship_profile_id !== null ||
    interaction.legacy_grant_id !== null ||
    !(
      (
        voiceProfileId === null &&
        voiceProfileVersion === null &&
        voiceProvider === null &&
        voiceModel === null &&
        voiceResourceId === null &&
        voiceProviderExpiresAt === null &&
        voiceSpeakerSha256 === null
      ) ||
      (
        typeof voiceProfileId === "string" &&
        voiceProfileId.trim() &&
        Number.isInteger(voiceProfileVersion) &&
        voiceProfileVersion >= 1 &&
        voiceProvider === "volcengine_doubao" &&
        voiceModel === "seed-icl-2.0" &&
        voiceResourceId === "seed-icl-2.0" &&
        typeof voiceProviderExpiresAt === "string" &&
        !Number.isNaN(new Date(voiceProviderExpiresAt).getTime()) &&
        /(Z|[+-]00:00)$/.test(voiceProviderExpiresAt) &&
        typeof voiceSpeakerSha256 === "string" &&
        /^[a-f0-9]{64}$/.test(voiceSpeakerSha256)
      )
    ) ||
    typeof interaction.fallback_voice_profile_id !== "string" ||
    !interaction.fallback_voice_profile_id.trim() ||
    interaction.fallback_voice_provider !== "volcengine_doubao" ||
    interaction.fallback_voice_model !== "seed-tts-2.0" ||
    interaction.fallback_voice_resource_id !== "seed-tts-2.0" ||
    !isJsonObject(capabilities) ||
    capabilities.conversation !== true ||
    capabilities.private_memory !== false ||
    capabilities.persona !== false ||
    capabilities.persona_low_sensitivity !== false ||
    capabilities.tools !== false ||
    capabilities.history !== false ||
    capabilities.learning !== false ||
    capabilities.voice_profile !== true
  ) {
    throw new Error("服务端没有返回可验证的数字分身预览模式，会话已停止");
  }
  return session;
}

export function getInteractionCapabilities() {
  return request("/v1/interaction/capabilities");
}

export async function createSession(
  userId,
  voiceBackend = "cascade",
  learningTaskId = null,
  {
    interactionMode = "companion",
    previewGrantId = null,
  } = {},
) {
  if (!["companion", "self_preview"].includes(interactionMode)) {
    throw new Error("H5 当前只支持陪伴模式或数字分身预览");
  }
  if (
    interactionMode === "self_preview" &&
    (typeof previewGrantId !== "string" || !previewGrantId.trim())
  ) {
    throw new Error("缺少服务端签发的数字分身预览授权");
  }
  const session = await request("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      user_id: userId,
      voice_backend: interactionMode === "self_preview" ? "cascade" : voiceBackend,
      interaction_mode: interactionMode,
      learning_task_id:
        interactionMode === "companion" ? learningTaskId : null,
      ...(interactionMode === "self_preview"
        ? { preview_grant_id: previewGrantId.trim() }
        : {}),
      locale: "zh-CN",
      client: {
        platform: "h5",
        timezone:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      },
    }),
  });
  return interactionMode === "self_preview"
    ? requireSelfPreviewInteraction(session)
    : requireCompanionInteraction(session);
}

export function exchangeOmniSdp(sessionId, offerSdp) {
  return request(
    `/v1/sessions/${encodeURIComponent(sessionId)}/omni/sdp`,
    {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: offerSdp,
    },
    { responseType: "text" },
  );
}

export function getAccessToken() {
  return activeIdentity?.access_token || null;
}

export function publishOmniTelemetry(sessionId, event) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/telemetry`, {
    method: "POST",
    body: JSON.stringify(event),
  });
}

export function stopResponse(sessionId) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/stop-response`, {
    method: "POST",
    body: JSON.stringify({ reason: "user_button" }),
  });
}

export function notifyRtcRecovered(sessionId) {
  return request(`/v1/sessions/${encodeURIComponent(sessionId)}/rtc-recovered`, {
    method: "POST",
  });
}

function createClientMessageId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return [...bytes]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .replace(/(.{8})(.{4})(.{4})(.{4})(.{12})/, "$1-$2-$3-$4-$5");
}

function ensureClientMessageId(message) {
  if (typeof message?.client_message_id === "string" && message.client_message_id) {
    return message;
  }
  message.client_message_id = createClientMessageId();
  return message;
}

export function saveMessage(message) {
  if (message?.history_eligible !== true) return Promise.resolve(null);
  const payload = { ...ensureClientMessageId(message) };
  delete payload.history_eligible;
  return request("/v1/memory/messages", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getMemoryDays(userId, limit = 14) {
  return request(
    `/v1/memory/days?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
  );
}

export function summarizeDay(userId, date) {
  return request(`/v1/memory/days/${encodeURIComponent(date)}/summary`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export function getLifeTimeline(limit = 30) {
  return request(`/v1/archive/life-timeline?limit=${limit}`);
}

export function searchLifeArchive(query, limit = 30) {
  return request(
    `/v1/archive/search?q=${encodeURIComponent(query.trim())}` +
      `&include_candidates=true&limit=${limit}`,
  );
}

export function getMemoryReviewQueue() {
  return request("/v1/archive/review-queue");
}

export function reviewMemoryClaim(claimId, action, correctedValue = null) {
  const payload = { action };
  if (action === "correct") payload.corrected_value = correctedValue;
  return request(`/v1/archive/memories/${encodeURIComponent(claimId)}/review`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent");
}

export function grantRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent", {
    method: "POST",
    body: JSON.stringify({
      policy_version: "raw-voice-archive-v1",
      retention_policy: "account_lifetime",
    }),
  });
}

export function revokeRawVoiceConsent() {
  return request("/v1/archive/raw-voice-consent", { method: "DELETE" });
}

export function getProfile(userId) {
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`);
}

export function updateProfile(userId, profile) {
  const payload = {};
  for (const field of [
    "display_name",
    "bio",
    "auto_summary",
    "voice_reply",
    "gentle_reminders",
    "reject_non_owner_voice",
    "companion_id",
  ]) {
    if (profile[field] !== undefined) payload[field] = profile[field];
  }
  if (profile.timezone !== undefined) payload.timezone = profile.timezone;
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getPersonaStatus() {
  return request("/v1/persona/status");
}

export function grantPersonaConsent() {
  return request("/v1/persona/consent", {
    method: "POST",
    body: JSON.stringify({
      accepted: true,
      policy_version: "persona-learning-v1",
    }),
  });
}

export function revokePersonaConsent() {
  return request("/v1/persona/consent", { method: "DELETE" });
}

export function getPersonaTraits() {
  return request("/v1/persona/traits");
}

export function reviewPersonaTrait(traitId, action, payload = {}) {
  return request(`/v1/persona/traits/${encodeURIComponent(traitId)}/review`, {
    method: "POST",
    body: JSON.stringify({ action, ...payload }),
  });
}

export function getPersonaVersions() {
  return request("/v1/persona/versions");
}

const growthDimensionKeys = new Set([
  "life_chapters",
  "important_people",
  "expression",
  "decision_cases",
  "relationship_models",
  "voice",
  "legacy",
]);
const growthDimensionStatuses = new Set([
  "empty",
  "emerging",
  "supported",
  "conflicted",
]);
const growthReadinessStatuses = new Set([
  "ready",
  "stale",
  "not_built",
  "dependency_pending",
]);
const growthTaskKinds = new Set([
  "natural_chat",
  "life_interview",
  "scenario_choice",
  "decision_review",
]);
const growthTaskStatuses = new Set([
  "draft",
  "active",
  "paused",
  "completed",
  "cancelled",
]);
const growthOwnerTargetKinds = new Set([
  "memory_claim",
  "persona_trait",
  "source_event",
  "digital_self_version",
  "person_entity",
  "timeline_entry",
  "relationship",
  "voice_profile",
  "cognitive_claim",
  "decision_case",
  "relationship_profile",
]);
const growthSourceWeights = new Set(["strong", "normal", "weak"]);

function invalidGrowthResponse(message = "成长地图响应无效") {
  throw new Error(message);
}

function requireGrowthString(value, message) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(message);
  }
  return value.trim();
}

function parseGrowthDate(value) {
  if (value === undefined || value === null) return value ?? null;
  const parsed = requireGrowthString(value, "成长地图时间字段无效");
  if (Number.isNaN(new Date(parsed).getTime())) {
    invalidGrowthResponse();
  }
  return parsed;
}

function parseGrowthSource(value) {
  if (!isJsonObject(value)) invalidGrowthResponse();
  const source = {
    event_id: requireGrowthString(value.event_id, "成长来源事件标识无效"),
    kind: requireGrowthString(value.kind, "成长来源类型无效"),
    event_type: requireGrowthString(value.event_type, "成长来源事件类型无效"),
  };
  for (const field of ["target_kind", "target_id", "label"]) {
    if (value[field] !== undefined && value[field] !== null) {
      source[field] = requireGrowthString(value[field], "成长来源字段无效");
    }
  }
  if (!growthSourceWeights.has(value.weight)) {
    invalidGrowthResponse();
  }
  source.weight = value.weight;
  if (value.occurred_at !== undefined) source.occurred_at = parseGrowthDate(value.occurred_at);
  return source;
}

function parseGrowthOverview(value) {
  if (!isJsonObject(value) || !Array.isArray(value.dimensions)) {
    invalidGrowthResponse();
  }
  return {
    dimensions: value.dimensions.map((dimension) => {
      if (
        !isJsonObject(dimension) ||
        !growthDimensionKeys.has(dimension.key) ||
        !growthDimensionStatuses.has(dimension.status) ||
        !Array.isArray(dimension.adopted_sources) ||
        !isJsonObject(dimension.rejected_reason_counts) ||
        !Array.isArray(dimension.conflicts) ||
        !Array.isArray(dimension.recent_changes) ||
        !Array.isArray(dimension.dependency_blockers) ||
        !isJsonObject(dimension.version_readiness)
      ) {
        invalidGrowthResponse();
      }
      const rejectedReasonCounts = {};
      for (const [reason, count] of Object.entries(dimension.rejected_reason_counts)) {
        if (!Number.isInteger(count) || count < 0) invalidGrowthResponse();
        rejectedReasonCounts[reason] = count;
      }
      const conflicts = dimension.conflicts.map((conflict) => {
        if (
          !isJsonObject(conflict) ||
          typeof conflict.target_kind !== "string" ||
          !conflict.target_kind ||
          typeof conflict.target_id !== "string" ||
          !conflict.target_id ||
          typeof conflict.event_id !== "string" ||
          !conflict.event_id ||
          typeof conflict.action !== "string" ||
          !conflict.action
        ) {
          invalidGrowthResponse();
        }
        return conflict;
      });
      const recentChanges = dimension.recent_changes.map((change) => {
        if (
          !isJsonObject(change) ||
          typeof change.event_id !== "string" ||
          !change.event_id ||
          typeof change.event_type !== "string" ||
          !change.event_type
        ) {
          invalidGrowthResponse();
        }
        if (typeof change.occurred_at !== "string") invalidGrowthResponse();
        return {
          ...change,
          occurred_at: parseGrowthDate(change.occurred_at),
        };
      });
      const readiness = dimension.version_readiness;
      if (
        !growthReadinessStatuses.has(readiness.status) ||
        (readiness.version_id !== undefined &&
          readiness.version_id !== null &&
          (typeof readiness.version_id !== "string" || !readiness.version_id))
      ) {
        invalidGrowthResponse();
      }
      return {
        key: dimension.key,
        status: dimension.status,
        adopted_sources: dimension.adopted_sources.map(parseGrowthSource),
        rejected_reason_counts: rejectedReasonCounts,
        conflicts,
        recent_changes: recentChanges,
        dependency_blockers: dimension.dependency_blockers.map((blocker) =>
          requireGrowthString(blocker, "成长依赖阻塞项无效"),
        ),
        version_readiness: {
          version_id: readiness.version_id || null,
          status: readiness.status,
        },
      };
    }),
  };
}

function parseGrowthTask(value) {
  if (
    !isJsonObject(value) ||
    typeof value.task_id !== "string" ||
    !value.task_id ||
    !growthTaskKinds.has(value.kind) ||
    !growthTaskStatuses.has(value.status) ||
    !Number.isInteger(value.revision) ||
    value.revision < 0 ||
    typeof value.created_at !== "string" ||
    !value.created_at ||
    Number.isNaN(new Date(value.created_at).getTime()) ||
    typeof value.updated_at !== "string" ||
    !value.updated_at ||
    Number.isNaN(new Date(value.updated_at).getTime())
  ) {
    invalidGrowthResponse("成长任务响应无效");
  }
  const task = {
    task_id: value.task_id,
    kind: value.kind,
    status: value.status,
    revision: value.revision,
    created_at: value.created_at,
    updated_at: value.updated_at,
  };
  for (const field of ["prompt_id", "prompt"]) {
    if (value[field] !== undefined && value[field] !== null) {
      task[field] = requireGrowthString(value[field], "成长任务提示字段无效");
    } else {
      task[field] = null;
    }
  }
  return task;
}

function parseGrowthTaskList(value) {
  if (!isJsonObject(value) || !Array.isArray(value.items)) {
    invalidGrowthResponse("成长任务列表响应无效");
  }
  return { items: value.items.map(parseGrowthTask) };
}

function requireGrowthTaskKind(kind) {
  const value = requireGrowthString(kind, "成长任务类型无效");
  if (!growthTaskKinds.has(value)) throw new Error("成长任务类型无效");
  return value;
}

function requireGrowthTaskStatus(status) {
  const value = requireGrowthString(status, "成长任务状态无效");
  if (!growthTaskStatuses.has(value)) throw new Error("成长任务状态无效");
  return value;
}

function requireGrowthRevision(revision) {
  if (!Number.isInteger(revision) || revision < 0) {
    throw new Error("成长任务版本号无效");
  }
  return revision;
}

export function getGrowthOverview() {
  return request("/v1/growth/overview").then(parseGrowthOverview);
}

export function getGrowthTasks() {
  return request("/v1/growth/tasks").then(parseGrowthTaskList);
}

export function createGrowthTask(eventId, kind, promptId = null) {
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    kind: requireGrowthTaskKind(kind),
  };
  if (promptId !== null && promptId !== undefined) {
    body.prompt_id = requireGrowthString(promptId, "成长提示标识无效");
  }
  return request("/v1/growth/tasks", {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

export function transitionGrowthTask(taskId, eventId, toStatus, expectedRevision) {
  const id = requireGrowthString(taskId, "成长任务标识无效");
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    to_status: requireGrowthTaskStatus(toStatus),
    expected_revision: requireGrowthRevision(expectedRevision),
  };
  return request(`/v1/growth/tasks/${encodeURIComponent(id)}/transitions`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

const growthResponseStructuredFields = new Set([
  "options",
  "constraints",
  "chosen_option",
  "rejected_options",
  "outcome",
  "reflection",
  "still_endorsed",
]);

function requireGrowthResponseList(value, message) {
  if (!Array.isArray(value) || value.length > 32) throw new Error(message);
  return value.map((item) => {
    const text = requireGrowthString(item, message);
    if (text.length > 1000) throw new Error(message);
    return text;
  });
}

function requireGrowthResponseText(value, message, maxLength) {
  const text = requireGrowthString(value, message);
  if (text.length > maxLength) throw new Error(message);
  return text;
}

function growthResponseStructuredBody(structured) {
  if (structured === undefined || structured === null) return null;
  if (!isJsonObject(structured)) throw new Error("成长任务结构化回答无效");
  const body = {};
  for (const [field, value] of Object.entries(structured)) {
    if (!growthResponseStructuredFields.has(field)) {
      throw new Error("成长任务结构化回答无效");
    }
    if (field === "options" || field === "constraints" || field === "rejected_options") {
      body[field] = requireGrowthResponseList(value, "成长任务选项无效");
    } else if (field === "chosen_option") {
      body[field] = requireGrowthResponseText(value, "成长任务已选方案无效", 1000);
    } else if (field === "outcome" || field === "reflection") {
      body[field] = requireGrowthResponseText(value, "成长任务复盘内容无效", 4000);
    } else if (typeof value === "boolean") {
      body[field] = value;
    } else {
      throw new Error("成长任务结构化回答无效");
    }
  }
  return Object.keys(body).length ? body : null;
}

export function respondGrowthTask(taskId, eventId, expectedRevision, answer, structured) {
  const id = requireGrowthString(taskId, "成长任务标识无效");
  if (typeof answer !== "string") throw new Error("请先写下你的回答");
  const structuredBody = growthResponseStructuredBody(structured);
  const trimmedAnswer = answer.trim();
  if (!trimmedAnswer && !structuredBody) throw new Error("请先写下你的回答");
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    expected_revision: requireGrowthRevision(expectedRevision),
    answer: trimmedAnswer,
    ...structuredBody,
  };
  return request(`/v1/growth/tasks/${encodeURIComponent(id)}/responses`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseGrowthTask);
}

export function reviewGrowthOwnerAction(eventId, action, targetKind, targetId) {
  const allowedActions = new Set(["not_me", "would_not_say"]);
  if (!allowedActions.has(action)) throw new Error("成长来源操作无效");
  const target = requireGrowthString(targetKind, "目标类型无效");
  if (!growthOwnerTargetKinds.has(target)) throw new Error("目标类型无效");
  return request("/v1/growth/owner-actions", {
    method: "POST",
    body: JSON.stringify({
      event_id: requireGrowthString(eventId, "成长事件标识无效"),
      action,
      target_kind: target,
      target_id: requireGrowthString(targetId, "目标标识无效"),
    }),
  }).then((value) => {
    if (!isJsonObject(value)) invalidGrowthResponse("成长来源操作响应无效");
    return value;
  });
}

const selfModelClaimTypes = new Set([
  "belief",
  "preference",
  "value",
  "decision_rule",
  "red_line",
  "uncertainty",
  "conflict",
  "support",
]);
const selfModelItemStatuses = new Set([
  "candidate",
  "confirmed",
  "disputed",
  "retracted",
  "superseded",
]);
const selfModelRelationshipStatuses = new Set([
  "candidate",
  "approved",
  "revoked",
  "superseded",
]);
const selfModelDecisionKinds = new Set(["real", "hypothetical"]);
const selfModelSourceRelations = new Set(["support", "counterexample"]);

function invalidSelfModelResponse() {
  throw new Error("认知与关系模型响应无效");
}

function parseSelfModelDate(value, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  if (typeof value !== "string" || Number.isNaN(new Date(value).getTime())) {
    invalidSelfModelResponse();
  }
  return value;
}

function parseSelfModelSource(value) {
  if (
    !isJsonObject(value) ||
    typeof value.source_event_id !== "string" ||
    !value.source_event_id ||
    !selfModelSourceRelations.has(value.relation) ||
    typeof value.adopted !== "boolean" ||
    typeof value.negative !== "boolean" ||
    value.speaker_class !== "owner" ||
    typeof value.excerpt !== "string"
  ) {
    invalidSelfModelResponse();
  }
  return {
    source_event_id: value.source_event_id,
    relation: value.relation,
    adopted: value.adopted,
    negative: value.negative,
    speaker_class: value.speaker_class,
    occurred_at: parseSelfModelDate(value.occurred_at),
    excerpt: value.excerpt,
  };
}

function parseSelfModelCommon(value, statuses) {
  if (
    !isJsonObject(value) ||
    !statuses.has(value.status) ||
    typeof value.sharing_scope !== "string" ||
    !value.sharing_scope ||
    typeof value.unresolved_conflict !== "boolean" ||
    !Array.isArray(value.sources) ||
    typeof value.step_up_verified !== "boolean" ||
    typeof value.effective !== "boolean" ||
    !Array.isArray(value.effective_reasons) ||
    value.effective_reasons.some((reason) => typeof reason !== "string" || !reason)
  ) {
    invalidSelfModelResponse();
  }
  return {
    status: value.status,
    sharing_scope: value.sharing_scope,
    unresolved_conflict: value.unresolved_conflict,
    sources: value.sources.map(parseSelfModelSource),
    owner_reviewed_at: parseSelfModelDate(value.owner_reviewed_at, {
      nullable: true,
    }),
    step_up_verified: value.step_up_verified,
    effective: value.effective,
    effective_reasons: [...value.effective_reasons],
    created_at: parseSelfModelDate(value.created_at),
  };
}

function parseSelfModelClaim(value) {
  const common = parseSelfModelCommon(value, selfModelItemStatuses);
  if (
    value.kind !== "cognitive_claim" ||
    typeof value.claim_id !== "string" ||
    !value.claim_id ||
    !selfModelClaimTypes.has(value.claim_type) ||
    typeof value.statement !== "string" ||
    !value.statement ||
    typeof value.context !== "string" ||
    typeof value.confidence !== "number" ||
    value.confidence < 0 ||
    value.confidence > 1 ||
    !Number.isInteger(value.version) ||
    value.version < 1
  ) {
    invalidSelfModelResponse();
  }
  return {
    ...value,
    ...common,
    updated_at: parseSelfModelDate(value.updated_at),
  };
}

function parseSelfModelDecisionCase(value) {
  const common = parseSelfModelCommon(value, selfModelItemStatuses);
  if (
    value.kind !== "decision_case" ||
    typeof value.case_id !== "string" ||
    !value.case_id ||
    !selfModelDecisionKinds.has(value.decision_kind) ||
    typeof value.context !== "string" ||
    !value.context ||
    !Array.isArray(value.options) ||
    !Array.isArray(value.constraints) ||
    typeof value.chosen_option !== "string" ||
    !Array.isArray(value.rejected_options) ||
    typeof value.outcome !== "string" ||
    typeof value.reflection !== "string" ||
    typeof value.still_endorsed !== "boolean" ||
    !Number.isInteger(value.version) ||
    value.version < 1
  ) {
    invalidSelfModelResponse();
  }
  return {
    ...value,
    ...common,
    updated_at: parseSelfModelDate(value.updated_at),
  };
}

function parseSelfModelRelationshipProfile(value) {
  const common = parseSelfModelCommon(value, selfModelRelationshipStatuses);
  if (
    value.kind !== "relationship_profile" ||
    typeof value.profile_id !== "string" ||
    !value.profile_id ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    typeof value.person_id !== "string" ||
    !value.person_id ||
    typeof value.relationship_id !== "string" ||
    !value.relationship_id ||
    typeof value.salutation !== "string" ||
    typeof value.tone !== "string" ||
    typeof value.advice_style !== "string" ||
    !Array.isArray(value.boundaries)
  ) {
    invalidSelfModelResponse();
  }
  return { ...value, ...common };
}

function parseSelfModel(value) {
  if (
    !isJsonObject(value) ||
    !Array.isArray(value.claims) ||
    !Array.isArray(value.decision_cases) ||
    !Array.isArray(value.relationship_profiles)
  ) {
    invalidSelfModelResponse();
  }
  return {
    claims: value.claims.map(parseSelfModelClaim),
    decision_cases: value.decision_cases.map(parseSelfModelDecisionCase),
    relationship_profiles: value.relationship_profiles.map(
      parseSelfModelRelationshipProfile,
    ),
  };
}

function requireSelfModelId(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label}无效`);
  }
  return value.trim();
}

function selfModelReviewBody(status, expectedVersion, idempotencyKey, password) {
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("认知材料版本无效");
  }
  const body = {
    status: requireSelfModelId(status, "审核状态"),
    expected_version: expectedVersion,
    idempotency_key: requireSelfModelId(idempotencyKey, "操作标识"),
  };
  if (password) body.password = password;
  return body;
}

export function getSelfModel() {
  return request("/v1/self-model").then(parseSelfModel);
}

export function addSelfModelClaimCounterexample(
  claimId,
  text,
  expectedVersion,
  eventId,
) {
  const id = requireSelfModelId(claimId, "认知主张标识");
  const counterexample = requireSelfModelId(text, "例外说明");
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("认知材料版本无效");
  }
  return request(
    `/v1/self-model/claims/${encodeURIComponent(id)}/counterexamples`,
    {
      method: "POST",
      body: JSON.stringify({
        event_id: requireSelfModelId(eventId, "操作标识"),
        expected_version: expectedVersion,
        text: counterexample,
      }),
    },
  ).then(parseSelfModelClaim);
}

export function reviewSelfModelClaim(
  claimId,
  status,
  expectedVersion,
  idempotencyKey,
  password = "",
) {
  const id = requireSelfModelId(claimId, "认知主张标识");
  return request(`/v1/self-model/claims/${encodeURIComponent(id)}/review`, {
    method: "POST",
    body: JSON.stringify(
      selfModelReviewBody(status, expectedVersion, idempotencyKey, password),
    ),
  }).then(parseSelfModelClaim);
}

export function reviewSelfModelDecisionCase(
  caseId,
  status,
  expectedVersion,
  idempotencyKey,
) {
  const id = requireSelfModelId(caseId, "决策案例标识");
  return request(
    `/v1/self-model/decision-cases/${encodeURIComponent(id)}/review`,
    {
      method: "POST",
      body: JSON.stringify(
        selfModelReviewBody(status, expectedVersion, idempotencyKey, ""),
      ),
    },
  ).then(parseSelfModelDecisionCase);
}

export function reviewSelfModelRelationshipProfile(
  profileId,
  versionNumber,
  status,
  expectedStatus,
  idempotencyKey,
  password,
) {
  const id = requireSelfModelId(profileId, "关系画像标识");
  if (!Number.isInteger(versionNumber) || versionNumber < 1) {
    throw new Error("关系画像版本无效");
  }
  return request(
    `/v1/self-model/relationship-profiles/${encodeURIComponent(id)}/versions/${versionNumber}/review`,
    {
      method: "POST",
      body: JSON.stringify({
        status: requireSelfModelId(status, "审核状态"),
        expected_status: requireSelfModelId(expectedStatus, "预期状态"),
        idempotency_key: requireSelfModelId(idempotencyKey, "操作标识"),
        password: requireSelfModelId(password, "账号密码"),
      }),
    },
  ).then(parseSelfModelRelationshipProfile);
}

const digitalSelfStatuses = new Set([
  "draft",
  "testing",
  "approved",
  "frozen",
  "revoked",
]);
const digitalSelfManifestSchemas = new Set([
  "digital-self-manifest-v1",
  "digital-self-manifest-v2",
  "digital-self-manifest-v3",
]);

function requireDigitalSelfVersionId(versionId) {
  if (typeof versionId !== "string" || !versionId.trim()) {
    throw new Error("数字分身版本标识无效");
  }
  return versionId.trim();
}

function requireDigitalSelfDigest(digest) {
  if (
    typeof digest !== "string" ||
    !/^[a-f0-9]{64}$/i.test(digest.trim())
  ) {
    throw new Error("数字分身版本摘要无效，请刷新后重试");
  }
  return digest.trim().toLowerCase();
}

function invalidDigitalSelfResponse() {
  throw new Error("数字分身版本响应无效");
}

function isJsonObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseNullableDigitalSelfId(value) {
  if (value === null) return null;
  if (typeof value !== "string" || !value.trim()) invalidDigitalSelfResponse();
  return value.trim();
}

function parseDigitalSelfSourceSummary(value, schemaVersion) {
  const isV2 = ["digital-self-manifest-v2", "digital-self-manifest-v3"].includes(
    schemaVersion,
  );
  const isV3 = schemaVersion === "digital-self-manifest-v3";
  const voiceProfile = parseDigitalSelfVoiceProfile(value?.voice_profile);
  if (
    !isJsonObject(value) ||
    !Number.isInteger(value.memory_claim_count) ||
    value.memory_claim_count < 0 ||
    !Number.isInteger(value.persona_trait_count) ||
    value.persona_trait_count < 0 ||
    (value.persona_version_id !== null &&
      (typeof value.persona_version_id !== "string" ||
        !value.persona_version_id.trim())) ||
    typeof value.source_summary_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(value.source_summary_sha256) ||
    (isV2 &&
      (!Number.isInteger(value.cognitive_claim_count) ||
        value.cognitive_claim_count < 0 ||
        !Number.isInteger(value.decision_case_count) ||
        value.decision_case_count < 0 ||
        !Number.isInteger(value.relationship_profile_count) ||
        value.relationship_profile_count < 0))
    ||
    (!isV3 && value.voice_profile !== undefined)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    ...(isV2
      ? {
          cognitive_claim_count: value.cognitive_claim_count,
          decision_case_count: value.decision_case_count,
          relationship_profile_count: value.relationship_profile_count,
        }
      : {}),
    persona_version_id: value.persona_version_id?.trim() || null,
    source_summary_sha256: value.source_summary_sha256.toLowerCase(),
    ...(isV3 ? { voice_profile: voiceProfile } : {}),
  };
}

function parseDigitalSelfVoiceProfile(value) {
  if (value === undefined || value === null) {
    return null;
  }
  if (
    !isJsonObject(value) ||
    typeof value.profile_id !== "string" ||
    !value.profile_id.trim() ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    value.provider !== "volcengine_doubao" ||
    value.target_model !== "seed-icl-2.0" ||
    value.resource_id !== "seed-icl-2.0" ||
    typeof value.provider_expires_at !== "string" ||
    Number.isNaN(new Date(value.provider_expires_at).getTime()) ||
    !/(Z|[+-]00:00)$/.test(value.provider_expires_at) ||
    typeof value.speaker_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.speaker_sha256)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    profile_id: value.profile_id.trim(),
    provider_expires_at: value.provider_expires_at.trim(),
    speaker_sha256: value.speaker_sha256,
  };
}

function sameDigitalSelfSourceSummary(left, right) {
  return (
    left.memory_claim_count === right.memory_claim_count &&
    left.persona_trait_count === right.persona_trait_count &&
    (left.cognitive_claim_count ?? 0) ===
      (right.cognitive_claim_count ?? 0) &&
    (left.decision_case_count ?? 0) === (right.decision_case_count ?? 0) &&
    (left.relationship_profile_count ?? 0) ===
      (right.relationship_profile_count ?? 0) &&
    left.persona_version_id === right.persona_version_id &&
    left.source_summary_sha256 === right.source_summary_sha256 &&
    JSON.stringify(left.voice_profile ?? null) ===
      JSON.stringify(right.voice_profile ?? null)
  );
}

function parseDigitalSelfVersion(value) {
  if (
    !isJsonObject(value) ||
    typeof value.version_id !== "string" ||
    !value.version_id ||
    !Number.isInteger(value.version_number) ||
    value.version_number < 1 ||
    !digitalSelfStatuses.has(value.status) ||
    typeof value.manifest_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/i.test(value.manifest_sha256) ||
    !isJsonObject(value.manifest) ||
    !digitalSelfManifestSchemas.has(value.manifest.schema_version) ||
    typeof value.manifest.compiler_version !== "string" ||
    !value.manifest.compiler_version ||
    typeof value.manifest.policy_version !== "string" ||
    !value.manifest.policy_version ||
    !Array.isArray(value.manifest.entries) ||
    typeof value.created_at !== "string" ||
    !value.created_at ||
    Number.isNaN(new Date(value.created_at).getTime())
  ) {
    invalidDigitalSelfResponse();
  }
  const parentVersionId = parseNullableDigitalSelfId(value.parent_version_id);
  const rollbackTargetVersionId = parseNullableDigitalSelfId(
    value.rollback_target_version_id,
  );
  const manifestParentVersionId = parseNullableDigitalSelfId(
    value.manifest.parent_version_id,
  );
  const manifestRollbackTargetVersionId = parseNullableDigitalSelfId(
    value.manifest.rollback_target_version_id,
  );
  const sourceSummary = parseDigitalSelfSourceSummary(
    value.source_summary,
    value.manifest.schema_version,
  );
  const manifestSourceSummary = parseDigitalSelfSourceSummary(
    value.manifest.source_summary,
    value.manifest.schema_version,
  );
  if (
    parentVersionId !== manifestParentVersionId ||
    rollbackTargetVersionId !== manifestRollbackTargetVersionId ||
    !sameDigitalSelfSourceSummary(sourceSummary, manifestSourceSummary)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    manifest_sha256: value.manifest_sha256.toLowerCase(),
    manifest: {
      ...value.manifest,
      parent_version_id: manifestParentVersionId,
      rollback_target_version_id: manifestRollbackTargetVersionId,
      source_summary: manifestSourceSummary,
    },
    source_summary: sourceSummary,
    parent_version_id: parentVersionId,
    rollback_target_version_id: rollbackTargetVersionId,
  };
}

function parseDigitalSelfVersionList(value) {
  if (!isJsonObject(value) || !Array.isArray(value.items)) {
    throw new Error("数字分身版本列表响应无效");
  }
  return {
    ...value,
    items: value.items.map(parseDigitalSelfVersion),
  };
}

export function getDigitalSelfVersions() {
  return request("/v1/digital-self/versions").then(parseDigitalSelfVersionList);
}

export function getDigitalSelfVersion(versionId) {
  const id = requireDigitalSelfVersionId(versionId);
  return request(`/v1/digital-self/versions/${encodeURIComponent(id)}`).then(
    parseDigitalSelfVersion,
  );
}

export function buildDigitalSelfVersion() {
  return request("/v1/digital-self/versions", {
    method: "POST",
  }).then(parseDigitalSelfVersion);
}

function transitionDigitalSelfVersion(
  versionId,
  action,
  { password = null, expectedManifestSha256 },
) {
  const id = requireDigitalSelfVersionId(versionId);
  const digest = requireDigitalSelfDigest(expectedManifestSha256);
  const body = { expected_manifest_sha256: digest };
  if (password !== null) {
    if (typeof password !== "string" || !password) {
      throw new Error("请输入当前账号密码确认");
    }
    body.password = password;
  }
  return request(`/v1/digital-self/versions/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
  }).then(parseDigitalSelfVersion);
}

export function beginDigitalSelfTesting(versionId, expectedManifestSha256) {
  return transitionDigitalSelfVersion(versionId, "testing", {
    expectedManifestSha256,
  });
}

export function approveDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "approve", {
    password,
    expectedManifestSha256,
  });
}

export function freezeDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "freeze", {
    password,
    expectedManifestSha256,
  });
}

export function revokeDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "revoke", {
    password,
    expectedManifestSha256,
  });
}

export function rollbackDigitalSelfVersion(
  versionId,
  password,
  expectedManifestSha256,
) {
  return transitionDigitalSelfVersion(versionId, "rollback", {
    password,
    expectedManifestSha256,
  });
}

const previewGrantStatuses = new Set(["active", "revoked", "expired"]);
const fidelityCategories = new Set([
  "fact",
  "decision",
  "relationship",
  "humor",
  "emotion",
  "unknown",
  "privacy",
]);
const fidelityStatuses = new Set(["active", "completed"]);
const fidelityVerdicts = new Set(["approve", "reject"]);

function invalidPreviewResponse(message = "数字分身预览响应无效") {
  throw new Error(message);
}

function previewString(value, message = "数字分身预览字段无效") {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(message);
  }
  return value.trim();
}

function previewDate(value) {
  const text = previewString(value, "数字分身预览时间无效");
  if (Number.isNaN(new Date(text).getTime())) invalidPreviewResponse();
  return text;
}

function parsePreviewVersionSummary(value) {
  if (
    !isJsonObject(value) ||
    !digitalSelfStatuses.has(value.status) ||
    typeof value.version_stale !== "boolean" ||
    typeof value.fidelity_eligible !== "boolean" ||
    typeof value.preview_eligible !== "boolean" ||
    (value.fidelity_verdict !== null &&
      !fidelityVerdicts.has(value.fidelity_verdict))
  ) {
    invalidPreviewResponse();
  }
  return {
    ...value,
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    version_stale: value.version_stale === true,
  };
}

function parseSelfPreviewCapability(value) {
  if (
    !isJsonObject(value) ||
    !["available", "blocked"].includes(value.status) ||
    typeof value.registered_owner !== "boolean" ||
    typeof value.active_owner_voice !== "boolean" ||
    !Array.isArray(value.missing) ||
    value.missing.some((item) => typeof item !== "string" || !item)
  ) {
    invalidPreviewResponse("数字分身预览能力响应无效");
  }
  const rawVersions = value.versions || value.available_versions || [];
  if (!Array.isArray(rawVersions)) invalidPreviewResponse();
  return {
    ...value,
    conversational: value.conversational === true,
    missing: [...value.missing],
    versions: rawVersions.map(parsePreviewVersionSummary),
  };
}

export function getSelfPreviewCapability() {
  return request("/v1/digital-self/preview-capability").then(
    parseSelfPreviewCapability,
  );
}

function parsePreviewGrant(value) {
  if (
    !isJsonObject(value) ||
    !previewGrantStatuses.has(value.status) ||
    !selfPreviewPerspectives.has(value.perspective) ||
    value.simulation_only !== true ||
    value.legacy_authority !== false
  ) {
    invalidPreviewResponse("数字分身预览授权响应无效");
  }
  return {
    ...value,
    grant_id: previewString(value.grant_id, "预览授权标识无效"),
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    expires_at: previewDate(value.expires_at),
    created_at: previewDate(value.created_at),
    used_at: value.used_at === null ? null : previewDate(value.used_at),
    revoked_at:
      value.revoked_at === null ? null : previewDate(value.revoked_at),
  };
}

export function issueSelfPreviewGrant({
  versionId,
  manifestSha256,
  perspective = "owner",
  password,
  idempotencyKey = createClientMessageId(),
}) {
  if (!selfPreviewPerspectives.has(perspective)) {
    throw new Error("数字分身预览视角无效");
  }
  if (typeof password !== "string" || password.length < 8) {
    throw new Error("请输入当前账号密码确认");
  }
  return request("/v1/digital-self/preview-grants", {
    method: "POST",
    body: JSON.stringify({
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      perspective,
      password,
      idempotency_key: previewString(idempotencyKey, "预览操作标识无效"),
    }),
  }).then(parsePreviewGrant);
}

export function revokeSelfPreviewGrant(grantId) {
  const id = previewString(grantId, "预览授权标识无效");
  return request(
    `/v1/digital-self/preview-grants/${encodeURIComponent(id)}/revoke`,
    { method: "POST" },
  ).then(parsePreviewGrant);
}

function parsePreviewSource(value) {
  if (
    !isJsonObject(value) ||
    typeof value.excerpt !== "string" ||
    value.excerpt.length > 400
  ) {
    invalidPreviewResponse("回答来源响应无效");
  }
  return {
    kind: previewString(value.kind, "回答来源类型无效"),
    item_id: previewString(value.item_id, "回答来源条目标识无效"),
    source_event_id: previewString(
      value.source_event_id,
      "回答来源事件标识无效",
    ),
    excerpt: value.excerpt,
  };
}

export function getSelfPreviewSources({
  sessionId,
  turnId,
  generationId,
  toolEpoch,
}) {
  const session = previewString(sessionId, "预览会话标识无效");
  for (const [label, value] of [
    ["话轮", turnId],
    ["生成", generationId],
    ["工具纪元", toolEpoch],
  ]) {
    if (!Number.isInteger(value) || value < 0) {
      throw new Error(`${label}标识无效`);
    }
  }
  return request(
    `/v1/digital-self/preview-sessions/${encodeURIComponent(session)}` +
      `/turns/${turnId}/generations/${generationId}/sources` +
      `?tool_epoch=${toolEpoch}`,
  ).then((value) => {
    if (
      !isJsonObject(value) ||
      value.session_id !== session ||
      value.turn_id !== turnId ||
      value.generation_id !== generationId ||
      value.tool_epoch !== toolEpoch ||
      !Array.isArray(value.items) ||
      value.items.length > 12
    ) {
      invalidPreviewResponse("回答来源响应无效");
    }
    return {
      ...value,
      items: value.items.map(parsePreviewSource),
    };
  });
}

export function submitSelfPreviewFeedback({
  sessionId,
  turnId,
  generationId,
  toolEpoch,
  versionId,
  manifestSha256,
  action,
  targetSourceEventIds,
  correctionText = null,
  eventId = createClientMessageId(),
  idempotencyKey = eventId,
}) {
  if (!["not_like_me", "correction"].includes(action)) {
    throw new Error("数字分身纠错动作无效");
  }
  if (
    !Array.isArray(targetSourceEventIds) ||
    !targetSourceEventIds.length ||
    targetSourceEventIds.length > 12
  ) {
    throw new Error("数字分身纠错来源无效");
  }
  if (
    action === "correction" &&
    (typeof correctionText !== "string" || !correctionText.trim())
  ) {
    throw new Error("请输入你的纠正内容");
  }
  return request("/v1/digital-self/preview-feedback", {
    method: "POST",
    body: JSON.stringify({
      event_id: previewString(eventId, "反馈事件标识无效"),
      idempotency_key: previewString(idempotencyKey, "反馈操作标识无效"),
      session_id: previewString(sessionId, "预览会话标识无效"),
      turn_id: turnId,
      generation_id: generationId,
      tool_epoch: toolEpoch,
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      action,
      target_source_event_ids: targetSourceEventIds.map((value) =>
        previewString(value, "反馈来源事件标识无效"),
      ),
      correction_text:
        action === "correction" ? correctionText.trim() : null,
    }),
  }).then((value) => {
    if (
      !isJsonObject(value) ||
      value.version_stale !== true ||
      value.rebuild_required !== true
    ) {
      invalidPreviewResponse("数字分身纠错响应无效");
    }
    return value;
  });
}

function parseFidelityTrial(value) {
  if (
    !isJsonObject(value) ||
    !fidelityCategories.has(value.category) ||
    typeof value.prompt !== "string" ||
    typeof value.slot_a !== "string" ||
    typeof value.slot_b !== "string" ||
    typeof value.available !== "boolean" ||
    (value.preferred_slot !== null &&
      !["a", "b"].includes(value.preferred_slot))
  ) {
    invalidPreviewResponse("忠实度保留题响应无效");
  }
  return {
    ...value,
    trial_id: previewString(value.trial_id, "忠实度保留题标识无效"),
    coverage_gap:
      value.coverage_gap === null
        ? null
        : previewString(value.coverage_gap, "忠实度覆盖缺口无效"),
  };
}

function parseFidelityEvaluation(value) {
  if (
    !isJsonObject(value) ||
    !fidelityStatuses.has(value.status) ||
    (value.verdict !== null && !fidelityVerdicts.has(value.verdict)) ||
    (value.status === "active" && value.verdict !== null) ||
    (value.status === "completed" && value.verdict === null) ||
    !Array.isArray(value.trials) ||
    !isJsonObject(value.summary) ||
    value.mapping_hidden !== true
  ) {
    invalidPreviewResponse("忠实度评测响应无效");
  }
  return {
    ...value,
    evaluation_id: previewString(value.evaluation_id, "忠实度评测标识无效"),
    version_id: requireDigitalSelfVersionId(value.version_id),
    manifest_sha256: requireDigitalSelfDigest(value.manifest_sha256),
    created_at: previewDate(value.created_at),
    completed_at:
      value.completed_at === null ? null : previewDate(value.completed_at),
    trials: value.trials.map(parseFidelityTrial),
  };
}

export function getFidelityEvaluations() {
  return request("/v1/digital-self/fidelity-evaluations").then((value) => {
    if (!isJsonObject(value) || !Array.isArray(value.items)) {
      invalidPreviewResponse("忠实度评测列表响应无效");
    }
    return { ...value, items: value.items.map(parseFidelityEvaluation) };
  });
}

export function getFidelityEvaluation(evaluationId) {
  const id = previewString(evaluationId, "忠实度评测标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(id)}`,
  ).then(parseFidelityEvaluation);
}

export function startFidelityEvaluation({
  versionId,
  manifestSha256,
  password = null,
  previewGrantId = null,
  idempotencyKey = createClientMessageId(),
}) {
  if ((password === null) === (previewGrantId === null)) {
    throw new Error("忠实度评测需要密码确认或有效预览授权");
  }
  return request("/v1/digital-self/fidelity-evaluations", {
    method: "POST",
    body: JSON.stringify({
      version_id: requireDigitalSelfVersionId(versionId),
      manifest_sha256: requireDigitalSelfDigest(manifestSha256),
      idempotency_key: previewString(idempotencyKey, "忠实度操作标识无效"),
      password,
      preview_grant_id: previewGrantId,
    }),
  }).then(parseFidelityEvaluation);
}

export function chooseFidelityTrial({
  evaluationId,
  trialId,
  preferredSlot,
  rationale = null,
}) {
  if (!["a", "b"].includes(preferredSlot)) {
    throw new Error("请选择回答 A 或 B");
  }
  const evaluation = previewString(evaluationId, "忠实度评测标识无效");
  const trial = previewString(trialId, "忠实度保留题标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(evaluation)}` +
      `/trials/${encodeURIComponent(trial)}/choice`,
    {
      method: "POST",
      body: JSON.stringify({
        preferred_slot: preferredSlot,
        rationale,
      }),
    },
  ).then(parseFidelityEvaluation);
}

export function completeFidelityEvaluation({
  evaluationId,
  verdict,
  rationale = null,
}) {
  if (!fidelityVerdicts.has(verdict)) {
    throw new Error("忠实度评测结论无效");
  }
  const id = previewString(evaluationId, "忠实度评测标识无效");
  return request(
    `/v1/digital-self/fidelity-evaluations/${encodeURIComponent(id)}/verdict`,
    {
      method: "POST",
      body: JSON.stringify({ verdict, rationale }),
    },
  ).then(parseFidelityEvaluation);
}

export function rollbackPersonaVersion(versionId) {
  return request(`/v1/persona/versions/${encodeURIComponent(versionId)}/rollback`, {
    method: "POST",
  });
}

export function getSpeakerProfiles() {
  return request("/v1/speakers");
}

export function enrollSpeakerProfiles(samples) {
  return request("/v1/speakers/enrollments", {
    method: "POST",
    body: JSON.stringify({
      consent_policy_version: "speaker-biometric-v1",
      consent_accepted: true,
      samples,
    }),
  });
}

export function revokeSpeakerProfile(profileId, reason = "用户在 H5 撤销声纹档案") {
  return request(`/v1/speakers/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
    body: JSON.stringify({ reason }),
  });
}

export function getVoiceProfiles() {
  return request("/v1/voices/profiles");
}

export function grantVoiceConsent() {
  return request("/v1/voices/consent", {
    method: "POST",
    body: JSON.stringify({
      accepted: true,
      policy_version: "voice-clone-v1",
    }),
  });
}

export function revokeVoiceConsent() {
  return request("/v1/voices/consent", { method: "DELETE" });
}

export function enrollVoiceProfile(sample) {
  return request("/v1/voices/enrollments", {
    method: "POST",
    body: JSON.stringify(sample),
  });
}

export function createVoiceBlindTrial(profileId) {
  return request(
    `/v1/voices/profiles/${encodeURIComponent(profileId)}/blind-trials`,
    { method: "POST" },
  );
}

export function previewVoiceBlindTrial(trialId, slot, text) {
  return request(
    `/v1/voices/blind-trials/${encodeURIComponent(trialId)}/preview`,
    {
      method: "POST",
      body: JSON.stringify({ slot, text }),
    },
    { responseType: "blob" },
  );
}

export function evaluateVoiceProfile(profileId, evaluation) {
  return request(
    `/v1/voices/profiles/${encodeURIComponent(profileId)}/evaluations`,
    {
      method: "POST",
      body: JSON.stringify(evaluation),
    },
  );
}

export function activateVoiceProfile(profileId) {
  return request(`/v1/voices/profiles/${encodeURIComponent(profileId)}/activate`, {
    method: "POST",
  });
}

export function revokeVoiceProfile(profileId) {
  return request(`/v1/voices/profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
}

export function exportAccountArchive(password) {
  return request("/v1/archive/exports", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

function clearDeletedIdentity() {
  const userId = activeIdentity?.user_id;
  if (userId) {
    window.localStorage.removeItem(`memoria:profile:${userId}`);
    window.localStorage.removeItem(pendingMessagesKey(userId));
    const legacyKey = "memoria:pending-messages";
    const remaining = readPendingMessages(legacyKey).filter(
      (message) => message?.user_id !== userId,
    );
    if (remaining.length) {
      window.localStorage.setItem(legacyKey, JSON.stringify(remaining));
    } else {
      window.localStorage.removeItem(legacyKey);
    }
  }
  activeIdentity = null;
  identityPromise = null;
  window.localStorage.removeItem(identityStorageKey);
  window.localStorage.removeItem(legacyIdentityStorageKey);
}

export async function deleteAccountData(password, confirmation) {
  const result = await request("/v1/archive/deletion-requests", {
    method: "POST",
    body: JSON.stringify({ password, confirmation }),
  });
  clearDeletedIdentity();
  return result;
}

function pendingMessagesKey(userId) {
  return `memoria:pending-messages:${userId}`;
}

function readPendingMessages(key) {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

export function cachePendingMessage(message) {
  if (!message?.user_id || message.history_eligible !== true) return;
  const key = pendingMessagesKey(message.user_id);
  const pending = readPendingMessages(key);
  pending.push(ensureClientMessageId(message));
  window.localStorage.setItem(key, JSON.stringify(pending.slice(-80)));
}

export async function flushPendingMessages() {
  const identity = activeIdentity;
  const userId = identity?.user_id;
  if (!userId) return;
  const isCurrentIdentity = () => activeIdentity === identity;
  const key = pendingMessagesKey(userId);
  const legacyKey = "memoria:pending-messages";
  const legacy = readPendingMessages(legacyKey);
  const pending = [
    ...readPendingMessages(key),
    ...legacy.filter((message) => message?.user_id === userId),
  ].map(ensureClientMessageId);
  const otherAccounts = legacy.filter((message) => message?.user_id !== userId);
  if (otherAccounts.length) {
    window.localStorage.setItem(legacyKey, JSON.stringify(otherAccounts));
  } else {
    window.localStorage.removeItem(legacyKey);
  }
  if (!pending.length) return;
  window.localStorage.setItem(key, JSON.stringify(pending));
  const failed = [];
  for (const message of pending) {
    if (!isCurrentIdentity()) return;
    try {
      await saveMessage(message);
    } catch {
      if (!isCurrentIdentity()) return;
      failed.push(message);
    }
  }
  if (!isCurrentIdentity()) return;
  window.localStorage.setItem(key, JSON.stringify(failed));
}
