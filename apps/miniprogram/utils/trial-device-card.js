const { sessionLimitsFromProfile } = require("./session-limits");

const UNREAD_SAFETY_COPY = "有一条需要你关注的对话";
const UNREAD_SAFETY_META_SUFFIX = "打开时首页会置顶显示";
const SAFETY_SUBSCRIBE_HELPER =
  "家长打开小程序时首页会置顶显示";

function usageFillPercent(usedMinutes, limitMinutes) {
  if (!Number.isInteger(usedMinutes) || usedMinutes < 0) return 0;
  if (!Number.isInteger(limitMinutes) || limitMinutes <= 0) return 0;
  return Math.min(100, Math.round((usedMinutes * 100) / limitMinutes));
}

function usageRingStyle(usedMinutes, limitMinutes) {
  const percent = usageFillPercent(usedMinutes, limitMinutes);
  return `background:conic-gradient(var(--mp-accent) 0% ${percent}%, var(--mp-track) ${percent}% 100%)`;
}

function usageCenterCopy(usedMinutes, limitMinutes) {
  const hasLimit = Number.isInteger(limitMinutes) && limitMinutes > 0;
  const known = Number.isInteger(usedMinutes) && usedMinutes >= 0;
  if (!hasLimit) return { usageCenterLabel: "未设置", usageCapLabel: "每天上限" };
  return {
    usageCenterLabel: known ? `${usageFillPercent(usedMinutes, limitMinutes)}%` : "",
    usageCapLabel: `上限 ${limitMinutes} 分钟`,
  };
}

function buildTrialDeviceCard({ online, runtimeProfile, usedMinutes } = {}) {
  const limits = sessionLimitsFromProfile(runtimeProfile);
  const usageKnown = Number.isInteger(usedMinutes) && usedMinutes >= 0;
  const usageUsedMinutes = usageKnown ? usedMinutes : null;
  const usageLimitMinutes = limits?.dailyChatMinutes ?? null;
  return {
    online: online === true,
    statusPill: online === true ? "在线" : "离线",
    statusLine: `${online === true ? "在线" : "离线"} · 已绑定`,
    usageKnown,
    usageUsedMinutes,
    usageLimitMinutes,
    usageFillPercent: usageFillPercent(usageUsedMinutes, usageLimitMinutes),
    usageRingStyle: usageRingStyle(usageUsedMinutes, usageLimitMinutes),
    ...usageCenterCopy(usageUsedMinutes, usageLimitMinutes),
    quietHoursLabel: limits?.quietHoursLabel || "",
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
  usageFillPercent,
  usageRingStyle,
  usageCenterCopy,
  memoryPreviewItems,
  offersSafetySubscribe,
  pinnedAlertMeta,
};
