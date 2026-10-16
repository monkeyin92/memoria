const EFFECTIVE_ON = "";

function releaseChip(effectiveOn = EFFECTIVE_ON) {
  const date = typeof effectiveOn === "string" ? effectiveOn.trim() : "";
  if (!date) return "内测版";
  return `内测版 · ${date}生效`;
}

module.exports = { EFFECTIVE_ON, releaseChip };
