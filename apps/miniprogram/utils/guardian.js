const EMOTION_LABELS = Object.freeze({
  neutral: "平静",
  happy: "愉快",
  sad: "有些低落",
  angry: "有些生气",
  fearful: "有些担心",
  disgusted: "有些不舒服",
  surprised: "惊喜",
});

const TOPIC_LABELS = Object.freeze({
  english: "英语练习",
  homework: "作业陪伴",
  school: "校园生活",
  interests: "兴趣爱好",
  family: "家庭日常",
  friends: "朋友相处",
  daily: "日常生活",
  other: "其他话题",
});

function firstArray(payload, keys) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function normalizeGuardianLinks(payload) {
  const links = firstArray(payload, ["items", "links"]);
  return links
    .filter((item) => item && typeof item === "object")
    .map((item, index) => {
      const minorUserId =
        typeof item.minor_user_id === "string" ? item.minor_user_id.trim() : "";
      const status = item.status === "active" || item.status === "pending" ? item.status : "";
      if (!minorUserId || !status) return null;
      const suppliedName =
        typeof item.minor_display_name === "string"
          ? item.minor_display_name.trim()
          : typeof item.display_name === "string"
            ? item.display_name.trim()
            : "";
      return {
        linkId: typeof item.link_id === "string" ? item.link_id : "",
        minorUserId,
        displayName: suppliedName || `孩子 ${index + 1}`,
        relation: item.relation === "legal_guardian" ? "监护人" : "家长",
        actorRole: item.actor_role === "minor" ? "minor" : "guardian",
        status,
        statusLabel: status === "active" ? "已绑定" : "等待孩子确认",
      };
    })
    .filter(Boolean);
}

function safeCount(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return 0;
  return Math.floor(value);
}

function distributionEntries(value, labels) {
  const counts = new Map();
  if (value && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, count] of Object.entries(value)) {
      if (labels[key]) counts.set(key, safeCount(count));
    }
  } else if (Array.isArray(value)) {
    for (const item of value) {
      if (!item || typeof item !== "object") continue;
      const key = item.key || item.label || item.emotion || item.topic;
      if (!labels[key]) continue;
      counts.set(key, safeCount(item.count ?? item.value));
    }
  }
  const total = [...counts.values()].reduce((sum, count) => sum + count, 0);
  return [...counts.entries()]
    .filter(([, count]) => count > 0)
    .sort((left, right) => right[1] - left[1])
    .map(([key, count]) => ({
      key,
      label: labels[key],
      count,
      width: `${Math.max(6, Math.round((count / total) * 100))}%`,
    }));
}

function isoDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "";
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value ? "" : value;
}

function displayDate(value) {
  const valid = isoDate(value);
  if (!valid) return "";
  const [, month, day] = valid.split("-");
  return `${Number(month)}月${Number(day)}日`;
}

function studyDurationLabel(minutes) {
  if (!minutes) return "0 分钟";
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  if (!hours) return `${remaining} 分钟`;
  return remaining ? `${hours} 小时 ${remaining} 分钟` : `${hours} 小时`;
}

function normalizeGuardianSummary(payload, expectedMinorUserId = "") {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new TypeError("成长小结响应无效");
  }
  const minorUserId =
    typeof payload.minor_user_id === "string" ? payload.minor_user_id.trim() : "";
  if (!minorUserId || (expectedMinorUserId && minorUserId !== expectedMinorUserId)) {
    throw new TypeError("成长小结账号不匹配");
  }

  const periodStart = isoDate(payload.window?.start || payload.period_start);
  const periodEnd = isoDate(payload.window?.end || payload.period_end);
  const emotionItems = distributionEntries(
    payload.emotion_distribution || payload.emotion_trend,
    EMOTION_LABELS,
  );
  const topicItems = distributionEntries(payload.topic_distribution, TOPIC_LABELS);
  const careDays = firstArray(payload.care_days, [])
    .map(isoDate)
    .filter(Boolean)
    .sort()
    .map((date) => ({ date, label: displayDate(date) }));
  const rawStudyMinutes = payload.study_minutes ?? payload.study_duration_minutes;
  const studyMinutes =
    typeof rawStudyMinutes === "number" && Number.isFinite(rawStudyMinutes)
      ? Math.max(0, Math.floor(rawStudyMinutes))
      : 0;
  const sourceEventCount =
    typeof payload.source_event_count === "number" && Number.isFinite(payload.source_event_count)
      ? Math.max(0, Math.floor(payload.source_event_count))
      : 0;

  return {
    minorUserId,
    periodStart,
    periodEnd,
    periodLabel:
      periodStart && periodEnd ? `${displayDate(periodStart)} 至 ${displayDate(periodEnd)}` : "最近一周",
    emotionItems,
    careDays,
    studyMinutes,
    studyDurationLabel: studyDurationLabel(studyMinutes),
    topicItems,
    sourceEventCount,
    hasData:
      sourceEventCount > 0 ||
      studyMinutes > 0 ||
      emotionItems.length > 0 ||
      careDays.length > 0 ||
      topicItems.length > 0,
  };
}

function guardianSummaryErrorState(error) {
  if (
    error?.status === 401 ||
    error?.status === 403 ||
    ["guardian_link_required", "guardian_consent_required"].includes(error?.code)
  ) {
    return "unauthorized";
  }
  if (error?.status === 404) return "empty";
  if (error?.status === 501 || error?.code === "guardian_summary_projection_unavailable") {
    return "unavailable";
  }
  return "error";
}

module.exports = {
  guardianSummaryErrorState,
  normalizeGuardianLinks,
  normalizeGuardianSummary,
};
