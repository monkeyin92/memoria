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

function usageChipLabel(usedMinutes, limitMinutes) {
  const hasLimit = Number.isInteger(limitMinutes) && limitMinutes > 0;
  const known = Number.isInteger(usedMinutes) && usedMinutes >= 0;
  if (!known) return hasLimit ? `用量暂未统计 · 每天上限 ${limitMinutes} 分钟` : "用量暂未统计";
  return hasLimit ? `今天聊了 ${usedMinutes} / ${limitMinutes} 分钟` : `今天聊了 ${usedMinutes} 分钟 · 未设上限`;
}

// 首页 hero 的角色状态：离线时灰着睡，安静时段睡着，其余时间动起来。
const HERO_STATES = {
  awake: { heroAnimated: true, heroMood: "default", heroDim: false, heroPill: "在线", heroPillClass: "pill-online" },
  quiet: { heroAnimated: false, heroMood: "sleepy", heroDim: false, heroPill: "安静时段", heroPillClass: "pill-amber" },
  offline: { heroAnimated: false, heroMood: "sleepy", heroDim: true, heroPill: "离线", heroPillClass: "pill-neutral" },
};

function heroState({ online, inQuietHours, quietHoursLabel } = {}) {
  const key = online !== true ? "offline" : inQuietHours ? "quiet" : "awake";
  const lines = {
    awake: "轻点机器人屏幕就能聊",
    quiet: quietHoursLabel ? `休息中 · ${quietHoursLabel}` : "休息中",
    offline: "设备连上网后回来，回顾照常可看",
  };
  return { heroState: key, ...HERO_STATES[key], heroLine: lines[key] };
}

// 首页“今天”一行：今天聊过就用今天的小结，没聊过就退到最近一条摘要。
function homeHighlight({ todayCount, todayOverview, memoryPreviews } = {}) {
  const latest = (Array.isArray(memoryPreviews) ? memoryPreviews : [])[0];
  if (todayCount > 0) {
    return { highlightLabel: `今天聊了 ${todayCount} 次`, highlightText: todayOverview || "具体内容在回顾里。" };
  }
  if (latest?.text) {
    const day = latest.dateLabel && latest.dateLabel !== "最近" ? ` · ${latest.dateLabel}` : "";
    return { highlightLabel: `最近一次${day}`, highlightText: latest.text };
  }
  return { highlightLabel: "今天还没聊", highlightText: "聊过之后，这里会出现一句小结。" };
}

function buildTrialDeviceCard({ online, runtimeProfile, usedMinutes, now } = {}) {
  const limits = sessionLimitsFromProfile(runtimeProfile, now);
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
    usageChipLabel: usageChipLabel(usageUsedMinutes, usageLimitMinutes),
    quietHoursLabel: limits?.quietHoursLabel || "",
    nightModeOn: Boolean(limits?.quietStart && limits?.quietEnd),
    ...heroState({
      online,
      inQuietHours: limits?.inQuietHours === true,
      quietHoursLabel: limits?.quietHoursLabel || "",
    }),
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
  heroState,
  homeHighlight,
  usageChipLabel,
  usageFillPercent,
  usageRingStyle,
  usageCenterCopy,
  memoryPreviewItems,
  offersSafetySubscribe,
  pinnedAlertMeta,
};
