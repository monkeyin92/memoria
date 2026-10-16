const EFFECTIVE_ON = "2026-10-16";

function releaseDate(effectiveOn = EFFECTIVE_ON) {
  return typeof effectiveOn === "string" ? effectiveOn.trim() : "";
}

function releaseChip(effectiveOn = EFFECTIVE_ON) {
  const date = releaseDate(effectiveOn);
  if (!date) return "内测版";
  return `内测版 · ${date}生效`;
}

function consentLocked(agreementPending, effectiveOn = EFFECTIVE_ON) {
  return Boolean(agreementPending) || !releaseDate(effectiveOn);
}

module.exports = { EFFECTIVE_ON, consentLocked, releaseChip };
