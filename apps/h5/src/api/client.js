import { withAbortTimeout } from "../network/abortTimeout.js";

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
  livekit_credentials_missing: "语音服务尚未配置，请联系管理员。",
  livekit_token_unavailable: "语音服务暂时不可用，请稍后重试。",
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
  legacy_resource_not_found: "传承授权或关系外壳不存在，或不属于当前账号。",
  legacy_access_denied: "当前账号没有这项传承访问权限。",
  legacy_snapshot_conflict: "传承授权已经变化，请刷新后重试。",
  legacy_idempotency_conflict: "同一传承操作标识已用于不同内容，请刷新后重试。",
  legacy_contract_invalid: "传承授权内容不完整或不符合冻结边界。",
  legacy_grantee_not_found: "接收人账号不存在或当前不可用。",
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
  {
    authenticated = true,
    responseType = "json",
    timeoutMs = null,
  } = {},
) {
  if (authenticated && !activeIdentity?.access_token) {
    throw new Error("账号身份尚未就绪");
  }
  const { signal: parentSignal, ...fetchOptions } = options;
  return withAbortTimeout(
    async (signal) => {
      const response = await fetch(`${baseUrl}${path}`, {
        ...fetchOptions,
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(authenticated
            ? { Authorization: `Bearer ${activeIdentity.access_token}` }
            : {}),
          ...fetchOptions.headers,
        },
        signal,
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
    },
    {
      timeoutMs,
      message: "请求超时，请稍后重试",
      signal: parentSignal,
    },
  );
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

export function clearActiveIdentity({ removeSnapshot = true } = {}) {
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

export async function request(
  path,
  options = {},
  {
    authenticated = true,
    responseType = "json",
    timeoutMs = null,
  } = {},
) {
  try {
    return await performRequest(path, options, {
      authenticated,
      responseType,
      timeoutMs,
    });
  } catch (error) {
    if (!authenticated || error?.status !== 401) throw error;
    await refreshAccess();
    return performRequest(path, options, {
      authenticated,
      responseType,
      timeoutMs,
    });
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

export async function registerAccount(username, password, displayName) {
  const identity = await request(
    "/v1/auth/register",
    {
      method: "POST",
      body: JSON.stringify({
        username,
        password,
        display_name: displayName,
      }),
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


export function getActiveIdentity() {
  return activeIdentity;
}

export function clearDeletedIdentityState() {
  activeIdentity = null;
  identityPromise = null;
  window.localStorage.removeItem(identityStorageKey);
  window.localStorage.removeItem(legacyIdentityStorageKey);
}

export function getAccessToken() {
  return activeIdentity?.access_token || null;
}
