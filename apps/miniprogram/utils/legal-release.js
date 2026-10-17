const EFFECTIVE_ON = "";

// 合规部 still has to assign real ids for the privacy guide and the guardian consent document.
const GUIDE_VERSION = "internal-test-unversioned";
const CONSENT_DOC_VERSION = "internal-test-unversioned";

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
