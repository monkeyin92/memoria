const baseUrl = (import.meta.env.VITE_CONTROL_API_URL || "/memoria-api").replace(
  /\/$/,
  "",
);

const identityStorageKey = "memoria:identity";
const legacyIdentityStorageKey = "memoria:anonymous-identity";
let activeIdentity = null;
let identityPromise = null;

function readStoredIdentity() {
  try {
    const raw =
      window.localStorage.getItem(identityStorageKey) ||
      window.localStorage.getItem(legacyIdentityStorageKey);
    const value = JSON.parse(raw || "null");
    if (
      value &&
      typeof value.user_id === "string" &&
      value.user_id &&
      typeof value.access_token === "string" &&
      value.access_token
    ) {
      return value;
    }
  } catch {
    // Invalid local identity data is ignored; the account gate will recover it.
  }
  return null;
}

async function request(
  path,
  options = {},
  { authenticated = true, responseType = "json" } = {},
) {
  if (authenticated && !activeIdentity?.access_token) {
    throw new Error("账号身份尚未就绪");
  }
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
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
    try {
      const parsed = JSON.parse(detail);
      if (typeof parsed?.detail === "string") message = parsed.detail;
    } catch {
      // Non-JSON upstream failures keep their safe response text.
    }
    const error = new Error(message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) return null;
  if (responseType === "blob") return response.blob();
  if (responseType === "text") return response.text();
  return response.json();
}

function persistIdentity(identity) {
  activeIdentity = {
    user_id: identity.user_id,
    username: identity.username || null,
    account_type: identity.account_type || "registered",
    access_token: identity.access_token,
  };
  window.localStorage.setItem(identityStorageKey, JSON.stringify(activeIdentity));
  window.localStorage.removeItem(legacyIdentityStorageKey);
  return activeIdentity;
}

export function bootstrapIdentity() {
  if (activeIdentity) return Promise.resolve(activeIdentity);
  if (identityPromise) return identityPromise;

  const stored = readStoredIdentity();
  identityPromise = (async () => {
    if (stored) {
      activeIdentity = stored;
      try {
        const current = await request("/v1/auth/me");
        if (current?.user_id === stored.user_id) {
          return persistIdentity({ ...stored, ...current });
        }
      } catch (error) {
        activeIdentity = null;
        if (![401, 403].includes(error?.status)) {
          throw error;
        }
      }
      activeIdentity = null;
      window.localStorage.removeItem(identityStorageKey);
      window.localStorage.removeItem(legacyIdentityStorageKey);
    }
    return null;
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
  return persistIdentity(identity);
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
  return persistIdentity(identity);
}

export function createSession(userId, voiceBackend = "cascade") {
  return request("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      user_id: userId,
      voice_backend: voiceBackend,
      locale: "zh-CN",
      client: {
        platform: "h5",
        timezone:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
      },
    }),
  });
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

export function saveMessage(message) {
  return request("/v1/memory/messages", {
    method: "POST",
    body: JSON.stringify(message),
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
  if (!message?.user_id) return;
  const key = pendingMessagesKey(message.user_id);
  const pending = readPendingMessages(key);
  pending.push(message);
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
  ];
  const otherAccounts = legacy.filter((message) => message?.user_id !== userId);
  if (otherAccounts.length) {
    window.localStorage.setItem(legacyKey, JSON.stringify(otherAccounts));
  } else {
    window.localStorage.removeItem(legacyKey);
  }
  if (!pending.length) return;
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
