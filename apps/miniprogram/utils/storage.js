const AUTH_KEY = "memoria:miniprogram:auth";
const LEGACY_IDENTITY_KEY = "memoria:miniprogram:identity";

function normalizeIdentity(value) {
  if (!value || typeof value.user_id !== "string" || !value.user_id) {
    return null;
  }
  return {
    user_id: value.user_id,
    username: typeof value.username === "string" && value.username ? value.username : null,
    account_type: value.account_type === "anonymous" ? "anonymous" : "registered",
    display_name:
      typeof value.display_name === "string" && value.display_name ? value.display_name : null,
    avatar_url: typeof value.avatar_url === "string" && value.avatar_url ? value.avatar_url : null,
  };
}

function normalizeAuthSnapshot(value) {
  const identity = normalizeIdentity(value?.identity || value);
  if (!identity) return null;
  return {
    identity,
    accessToken: typeof value?.accessToken === "string" ? value.accessToken : "",
    expiresAt: Number.isFinite(value?.expiresAt) ? Number(value.expiresAt) : 0,
  };
}

function readAuthSnapshot() {
  try {
    const current = normalizeAuthSnapshot(wx.getStorageSync(AUTH_KEY));
    if (current) return current;
    return normalizeAuthSnapshot(wx.getStorageSync(LEGACY_IDENTITY_KEY));
  } catch {
    return null;
  }
}

function writeAuthSnapshot(snapshot) {
  const normalized = normalizeAuthSnapshot(snapshot);
  if (!normalized) throw new Error("账号身份响应无效");
  wx.setStorageSync(AUTH_KEY, normalized);
  try {
    wx.removeStorageSync(LEGACY_IDENTITY_KEY);
  } catch {
    // Legacy cleanup is best effort.
  }
}

function clearAuthSnapshot() {
  try {
    wx.removeStorageSync(AUTH_KEY);
    wx.removeStorageSync(LEGACY_IDENTITY_KEY);
  } catch {
    // Storage cleanup is best effort; in-memory auth is already cleared.
  }
}

module.exports = {
  normalizeIdentity,
  normalizeAuthSnapshot,
  readAuthSnapshot,
  writeAuthSnapshot,
  clearAuthSnapshot,
};
