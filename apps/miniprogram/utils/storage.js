const IDENTITY_KEY = "memoria:miniprogram:identity";

function normalizeIdentity(value) {
  if (!value || typeof value.user_id !== "string" || !value.user_id) {
    return null;
  }
  return {
    user_id: value.user_id,
    username: typeof value.username === "string" && value.username ? value.username : null,
    account_type: value.account_type === "anonymous" ? "anonymous" : "registered",
  };
}

function readIdentitySnapshot() {
  try {
    return normalizeIdentity(wx.getStorageSync(IDENTITY_KEY));
  } catch {
    return null;
  }
}

function writeIdentitySnapshot(identity) {
  const normalized = normalizeIdentity(identity);
  if (!normalized) {
    throw new Error("账号身份响应无效");
  }
  wx.setStorageSync(IDENTITY_KEY, normalized);
}

function clearIdentitySnapshot() {
  try {
    wx.removeStorageSync(IDENTITY_KEY);
  } catch {
    // Storage cleanup is best effort; in-memory auth is already cleared.
  }
}

module.exports = {
  normalizeIdentity,
  readIdentitySnapshot,
  writeIdentitySnapshot,
  clearIdentitySnapshot,
};
