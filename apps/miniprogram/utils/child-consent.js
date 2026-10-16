const CHILD_INFO_CONSENT_KEY = "memoria:miniprogram:child-info-consent";

function hasChildInfoConsent() {
  try {
    return wx.getStorageSync(CHILD_INFO_CONSENT_KEY) === "accepted";
  } catch {
    return false;
  }
}

function grantChildInfoConsent() {
  wx.setStorageSync(CHILD_INFO_CONSENT_KEY, "accepted");
}

function ensureChildInfoConsent(redirect) {
  if (hasChildInfoConsent()) return true;
  const target = typeof redirect === "string" ? redirect : "";
  wx.navigateTo({
    url: `/pages/child-consent/index?redirect=${encodeURIComponent(target)}`,
  });
  return false;
}

module.exports = {
  CHILD_INFO_CONSENT_KEY,
  hasChildInfoConsent,
  grantChildInfoConsent,
  ensureChildInfoConsent,
};
