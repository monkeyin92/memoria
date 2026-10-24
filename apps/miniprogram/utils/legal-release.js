const EFFECTIVE_ON = "2026年10月24日";

// 页面用户可见文本一改就升 r 号，旧号不复用。仅内测用。接真实家庭前换正式号，并改为服务端记录。
const GUIDE_VERSION = "internal-test-guide-20261017-r5";
const CONSENT_DOC_VERSION = "internal-test-consent-20261017-r5";

// Experience-build self-test flips this source constant locally before upload and must not commit it.
// check:release-gate fails when the constant is true, so that commit cannot merge to main.
const INTERNAL_TEST_CONSENT_UNLOCK = false;

function releaseDate(effectiveOn = EFFECTIVE_ON) {
  return typeof effectiveOn === "string" ? effectiveOn.trim() : "";
}

function releaseChip(effectiveOn = EFFECTIVE_ON) {
  const date = releaseDate(effectiveOn);
  if (!date) return "内测版";
  return `内测版 · ${date}生效`;
}

function consentLocked(
  agreementPending,
  effectiveOn = EFFECTIVE_ON,
  unlock = INTERNAL_TEST_CONSENT_UNLOCK,
) {
  if (unlock === true) return false;
  return Boolean(agreementPending) || !releaseDate(effectiveOn);
}

module.exports = {
  EFFECTIVE_ON,
  GUIDE_VERSION,
  CONSENT_DOC_VERSION,
  INTERNAL_TEST_CONSENT_UNLOCK,
  consentLocked,
  releaseChip,
};
