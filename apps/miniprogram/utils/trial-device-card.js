/*
 * Trial home device card + safety banner fields.
 * Usage minutes come from Runtime Profile limits; used minutes stay 0 until a
 * daily tally exists on the server (product still enforces per-session max).
 */

const { sessionLimitsFromProfile } = require("./session-limits");

const UNREAD_SAFETY_COPY = "有一条安全提醒未读";
const USAGE_LABEL_PREFIX = "今日已用 / ";

function formatQuietHoursLine(sessionLimits) {
  if (!sessionLimits?.quietStart || !sessionLimits?.quietEnd) return "";
  return `夜间 ${sessionLimits.quietStart}–${sessionLimits.quietEnd}`;
}

function usageLabel(limitMinutes) {
  const limit = Number.isInteger(limitMinutes) && limitMinutes > 0 ? limitMinutes : 30;
  return `${USAGE_LABEL_PREFIX}${limit} 分钟`;
}

function buildTrialDeviceCard({ online, runtimeProfile, usedMinutes = 0 } = {}) {
  const limits = sessionLimitsFromProfile(runtimeProfile);
  const limitMinutes =
    limits?.maxSessionMinutes && limits.maxSessionMinutes > 0 ? limits.maxSessionMinutes : 30;
  const used =
    Number.isInteger(usedMinutes) && usedMinutes >= 0 ? usedMinutes : 0;
  return {
    online: online === true,
    statusPill: online === true ? "在线" : "离线",
    usageUsedMinutes: used,
    usageLimitMinutes: limitMinutes,
    usageLabel: usageLabel(limitMinutes),
    quietHoursLine: formatQuietHoursLine(limits) || "夜间 21:00–07:00",
  };
}

function hasUnreadSafetyAlert(items) {
  return (Array.isArray(items) ? items : []).some((item) => {
    if (!item || typeof item !== "object") return false;
    if (item.pinned === true) return true;
    const status = typeof item.delivery_status === "string" ? item.delivery_status : "";
    return status === "no_subscription" || status === "failed" || status === "pending";
  });
}

function offersSafetySubscribe(binding) {
  if (!binding || (binding.status && binding.status !== "active")) return false;
  return (
    binding.declared_mode === "parent_for_child" ||
    binding.declared_mode === "child_for_parent"
  );
}

module.exports = {
  UNREAD_SAFETY_COPY,
  buildTrialDeviceCard,
  formatQuietHoursLine,
  hasUnreadSafetyAlert,
  offersSafetySubscribe,
  usageLabel,
};
