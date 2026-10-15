/*
 * Parent home v3 board fields.
 * Limit minutes come from Runtime Profile MAX_SESSION_SECONDS.
 * Used minutes show only when a real tally is passed in, never a fake 0.
 * No battery fields.
 */

const { sessionLimitsFromProfile } = require("./session-limits");

const UNREAD_SAFETY_COPY = "有一条需要你关注的对话";
const UNREAD_SAFETY_META_SUFFIX = "打开时首页会置顶显示";
const SAFETY_SUBSCRIBE_HELPER =
  "家长打开小程序时首页会置顶显示";

function formatQuietHoursLine(sessionLimits) {
  if (!sessionLimits?.quietStart || !sessionLimits?.quietEnd) return "";
  return `夜间 ${sessionLimits.quietStart}–${sessionLimits.quietEnd}`;
}

function buildTrialDeviceCard({ online, runtimeProfile, usedMinutes } = {}) {
  const limits = sessionLimitsFromProfile(runtimeProfile);
  const limitMinutes =
    limits?.maxSessionMinutes && limits.maxSessionMinutes > 0 ? limits.maxSessionMinutes : 30;
  const usageKnown = Number.isInteger(usedMinutes) && usedMinutes >= 0;
  const quietHoursLine = formatQuietHoursLine(limits) || "夜间 21:00–07:00";
  return {
    online: online === true,
    statusPill: online === true ? "在线" : "离线",
    statusLine: `${online === true ? "在线" : "离线"} · 已绑定`,
    usageKnown,
    usageUsedMinutes: usageKnown ? usedMinutes : null,
    usageLimitMinutes: limitMinutes,
    quietHoursLine,
    quietHoursLabel: limits?.quietHoursLabel || "21:00 – 07:00",
    nightModeOn: Boolean(limits?.quietStart && limits?.quietEnd),
  };
}

function alertTimeLabel(occurredAt, now = new Date()) {
  const at = new Date(typeof occurredAt === "string" ? occurredAt : NaN);
  if (Number.isNaN(at.getTime())) return "";
  const clock = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  return at.toDateString() === now.toDateString()
    ? `今天 ${clock}`
    : `${at.getMonth() + 1}月${at.getDate()}日 ${clock}`;
}

function pinnedAlertMeta({ childName, occurredAt } = {}) {
  const parts = [];
  if (typeof childName === "string" && childName.trim()) parts.push(childName.trim());
  if (typeof occurredAt === "string" && occurredAt.trim()) parts.push(occurredAt.trim());
  parts.push(UNREAD_SAFETY_META_SUFFIX);
  return parts.join(" · ");
}

function memoryPreviewItems(summaries, limit = 3) {
  return (Array.isArray(summaries) ? summaries : [])
    .slice(0, limit)
    .map((item) => ({
      recordId: item.recordId,
      text: item.text,
      dateLabel: item.dateLabel || "",
    }));
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
  UNREAD_SAFETY_META_SUFFIX,
  SAFETY_SUBSCRIBE_HELPER,
  alertTimeLabel,
  buildTrialDeviceCard,
  formatQuietHoursLine,
  hasUnreadSafetyAlert,
  memoryPreviewItems,
  offersSafetySubscribe,
  pinnedAlertMeta,
};
