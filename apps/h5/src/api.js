const baseUrl = (import.meta.env.VITE_CONTROL_API_URL || "/memoria-api").replace(
  /\/$/,
  "",
);

const identityStorageKey = "memoria:anonymous-identity";
let activeIdentity = null;
let identityPromise = null;

function readStoredIdentity() {
  try {
    const value = JSON.parse(window.localStorage.getItem(identityStorageKey) || "null");
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
    // Invalid or legacy identity data is replaced by a fresh anonymous identity.
  }
  return null;
}

async function request(
  path,
  options = {},
  { authenticated = true, responseType = "json" } = {},
) {
  if (authenticated && !activeIdentity?.access_token) {
    throw new Error("匿名身份尚未就绪");
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
  if (responseType === "text") return response.text();
  return response.json();
}

async function issueAnonymousIdentity() {
  const identity = await request(
    "/v1/auth/anonymous",
    {
      method: "POST",
      body: JSON.stringify({
        client: {
          platform: "h5",
          timezone:
            Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
        },
      }),
    },
    { authenticated: false },
  );
  if (
    !identity ||
    typeof identity.user_id !== "string" ||
    !identity.user_id ||
    typeof identity.access_token !== "string" ||
    !identity.access_token
  ) {
    throw new Error("匿名身份响应无效");
  }
  activeIdentity = {
    user_id: identity.user_id,
    access_token: identity.access_token,
  };
  window.localStorage.setItem(
    identityStorageKey,
    JSON.stringify(activeIdentity),
  );
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
        if (current?.user_id === stored.user_id) return stored;
      } catch (error) {
        activeIdentity = null;
        if (![401, 403].includes(error?.status)) {
          throw error;
        }
      }
      activeIdentity = null;
      window.localStorage.removeItem(identityStorageKey);
    }
    return issueAnonymousIdentity();
  })().finally(() => {
    identityPromise = null;
  });
  return identityPromise;
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

export function getProfile(userId) {
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`);
}

export function updateProfile(userId, profile) {
  const payload = {
    display_name: profile.display_name,
    bio: profile.bio,
    auto_summary: profile.auto_summary,
    voice_reply: profile.voice_reply,
    gentle_reminders: profile.gentle_reminders,
    timezone:
      profile.timezone ||
      Intl.DateTimeFormat().resolvedOptions().timeZone ||
      "Asia/Shanghai",
  };
  return request(`/v1/memory/profile/${encodeURIComponent(userId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function cachePendingMessage(message) {
  const key = "memoria:pending-messages";
  const pending = JSON.parse(window.localStorage.getItem(key) || "[]");
  pending.push(message);
  window.localStorage.setItem(key, JSON.stringify(pending.slice(-80)));
}

export async function flushPendingMessages() {
  const key = "memoria:pending-messages";
  const pending = JSON.parse(window.localStorage.getItem(key) || "[]");
  if (!pending.length) return;
  const failed = [];
  for (const message of pending) {
    try {
      await saveMessage(message);
    } catch {
      failed.push(message);
    }
  }
  window.localStorage.setItem(key, JSON.stringify(failed));
}
