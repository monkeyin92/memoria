/*
 * 孩子模式的使用时段（P0-04 D3）：监护人在绑定时设定的夜间休息时间与单次时长。
 * 只读已签名 Runtime Profile 里的义务（QUIET_HOURS / MAX_SESSION_SECONDS），
 * 这就是设备实际执行的值；profile 无效、不是未成年人或缺义务时不展示。
 */

const QUIET_HOUR_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;
// 设备按北京时间执行休息时段（agent 的 MEMORIA_TIMEZONE 默认 Asia/Shanghai）。
const DEVICE_UTC_OFFSET_MINUTES = 8 * 60;

function obligationParams(profile, code) {
  const list = Array.isArray(profile?.obligations) ? profile.obligations : [];
  const hit = list.find((item) => item && typeof item === "object" && item.code === code);
  return hit && hit.params && typeof hit.params === "object" ? hit.params : null;
}

function minutesOf(clock) {
  const [hours, minutes] = clock.split(":").map(Number);
  return hours * 60 + minutes;
}

function deviceMinutesNow(now) {
  const utc = now.getUTCHours() * 60 + now.getUTCMinutes();
  return (utc + DEVICE_UTC_OFFSET_MINUTES) % (24 * 60);
}

/* [start, end) 可跨午夜；start === end 表示没有休息时段，与 agent 的 in_quiet_hours 一致。 */
function inQuietHours(window, now) {
  const start = minutesOf(window[0]);
  const end = minutesOf(window[1]);
  if (start === end) return false;
  const current = deviceMinutesNow(now);
  return start < end ? current >= start && current < end : current >= start || current < end;
}

function sessionLimitsFromProfile(profile, now = new Date()) {
  if (!profile || profile.valid !== true || profile.subject_category !== "minor") return null;
  const quiet = obligationParams(profile, "QUIET_HOURS")?.quiet_hours;
  const maxSeconds = obligationParams(profile, "MAX_SESSION_SECONDS")?.max_session_seconds;
  const hasQuiet =
    Array.isArray(quiet) &&
    quiet.length === 2 &&
    quiet.every((item) => typeof item === "string" && QUIET_HOUR_PATTERN.test(item)) &&
    quiet[0] !== quiet[1];
  const hasMax = Number.isInteger(maxSeconds) && maxSeconds > 0;
  if (!hasQuiet && !hasMax) return null;
  const crossesMidnight = hasQuiet && minutesOf(quiet[0]) > minutesOf(quiet[1]);
  return {
    // 原始值供「修改」抽屉回填；展示用 label。
    maxSessionMinutes: hasMax ? Math.max(1, Math.round(maxSeconds / 60)) : null,
    quietStart: hasQuiet ? quiet[0] : "",
    quietEnd: hasQuiet ? quiet[1] : "",
    quietHoursLabel: hasQuiet ? `${quiet[0]} – ${crossesMidnight ? "次日 " : ""}${quiet[1]}` : "",
    inQuietHours: hasQuiet ? inQuietHours(quiet, now) : false,
    maxSessionLabel: hasMax ? `${Math.max(1, Math.round(maxSeconds / 60))} 分钟` : "",
  };
}

module.exports = { sessionLimitsFromProfile, inQuietHours };
