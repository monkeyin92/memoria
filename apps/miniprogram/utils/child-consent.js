const { GUIDE_VERSION, CONSENT_DOC_VERSION } = require("./legal-release");

const CHILD_INFO_CONSENT_KEY = "memoria:miniprogram:child-info-consent";
const CHILD_INFO_CONSENT_VERSIONS_KEY = "memoria:miniprogram:child-info-consent-versions";

// 存的值带上当时的版本号。只存 "accepted" 的话，文案改了旧手机也不会被要求重新同意。
function consentStamp() {
  return `accepted:${GUIDE_VERSION}:${CONSENT_DOC_VERSION}`;
}

function hasChildInfoConsent() {
  try {
    return wx.getStorageSync(CHILD_INFO_CONSENT_KEY) === consentStamp();
  } catch {
    return false;
  }
}

function grantChildInfoConsent() {
  wx.setStorageSync(CHILD_INFO_CONSENT_KEY, consentStamp());
}

function recordInternalTestConsentVersions(viaUnlock) {
  if (viaUnlock !== true) return;
  wx.setStorageSync(CHILD_INFO_CONSENT_VERSIONS_KEY, {
    guideVersion: GUIDE_VERSION,
    consentDocVersion: CONSENT_DOC_VERSION,
    acceptedAt: new Date().toISOString(),
    unlock: "internal-test",
  });
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
  CHILD_INFO_CONSENT_VERSIONS_KEY,
  consentStamp,
  hasChildInfoConsent,
  grantChildInfoConsent,
  recordInternalTestConsentVersions,
  ensureChildInfoConsent,
};
