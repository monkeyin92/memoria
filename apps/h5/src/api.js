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
  if (
    interaction?.interaction_mode !== "companion" ||
    typeof interaction.mode_policy_version !== "string" ||
    !interaction.mode_policy_version ||
    typeof interaction.companion_style_id !== "string" ||
    !interaction.companion_style_id ||
    typeof interaction.companion_style_version !== "string" ||
    !interaction.companion_style_version ||
    interaction.digital_self_version_id !== null ||
    interaction.relationship_profile_id !== null ||
    interaction.legacy_grant_id !== null
  ) {
    throw new Error("服务端没有返回可验证的陪伴模式，会话已停止");
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
) {
  const session = await request("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      user_id: userId,
      voice_backend: voiceBackend,
      interaction_mode: "companion",
      learning_task_id: learningTaskId,
      locale: "zh-CN",
      client: {
        platform: "h5",
        timezone:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      },
    }),
  });
  return requireCompanionInteraction(session);
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

export function respondGrowthTask(taskId, eventId, expectedRevision, answer) {
  const id = requireGrowthString(taskId, "成长任务标识无效");
  const body = {
    event_id: requireGrowthString(eventId, "成长事件标识无效"),
    expected_revision: requireGrowthRevision(expectedRevision),
    answer: requireGrowthString(answer, "请先写下你的回答"),
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

const digitalSelfStatuses = new Set([
  "draft",
  "testing",
  "approved",
  "frozen",
  "revoked",
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

function parseDigitalSelfSourceSummary(value) {
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
    !/^[a-f0-9]{64}$/i.test(value.source_summary_sha256)
  ) {
    invalidDigitalSelfResponse();
  }
  return {
    ...value,
    persona_version_id: value.persona_version_id?.trim() || null,
    source_summary_sha256: value.source_summary_sha256.toLowerCase(),
  };
}

function sameDigitalSelfSourceSummary(left, right) {
  return (
    left.memory_claim_count === right.memory_claim_count &&
    left.persona_trait_count === right.persona_trait_count &&
    left.persona_version_id === right.persona_version_id &&
    left.source_summary_sha256 === right.source_summary_sha256
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
    typeof value.manifest.schema_version !== "string" ||
    !value.manifest.schema_version ||
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
  const sourceSummary = parseDigitalSelfSourceSummary(value.source_summary);
  const manifestSourceSummary = parseDigitalSelfSourceSummary(
    value.manifest.source_summary,
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
