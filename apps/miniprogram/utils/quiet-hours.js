/**
 * Guardian quiet-hours window on the bind form and device settings sheet.
 * Duration is cross-midnight aware. Windows shorter than 1 hour or longer
 * than 12 hours ask for an explicit confirm before submit.
 */

// The window services/policy/engine.py signs by default. A Python test reads this line.
const DEFAULT_QUIET_HOURS = Object.freeze({ start: "21:00", end: "07:00" });
const MIN_USUAL_MINUTES = 60;
const MAX_USUAL_MINUTES = 12 * 60;
const EN_DASH = "\u2013";

function minutesOf(clock) {
  const [hours, minutes] = String(clock).split(":").map(Number);
  return hours * 60 + minutes;
}

/** Inclusive open window length in minutes; same start/end is 0 (no rest). */
function quietHoursDurationMinutes(start, end) {
  const from = minutesOf(start);
  const to = minutesOf(end);
  if (from === to) return 0;
  return to > from ? to - from : 24 * 60 - from + to;
}

function quietHoursNeedsLengthConfirm(start, end) {
  const duration = quietHoursDurationMinutes(start, end);
  return duration > 0 && (duration < MIN_USUAL_MINUTES || duration > MAX_USUAL_MINUTES);
}

/** Exact default copy: 夜间模式：21:00–07:00（这段时间点屏只会说晚安） */
function nightModeQuietHoursLabel(start, end = undefined) {
  const window =
    typeof start === "object" && start !== null
      ? start
      : { start, end };
  return `夜间模式：${window.start}${EN_DASH}${window.end}（这段时间点屏只会说晚安）`;
}

function formatDurationForConfirm(minutes) {
  if (minutes % 60 === 0) return `${minutes / 60} 小时`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} 分钟`;
  return `${hours} 小时 ${rest} 分钟`;
}

/**
 * Resolve true when the window is usual, or the guardian confirms an unusual one.
 * Cancel / fail leaves the form open and does not submit.
 */
function confirmUnusualQuietHours({ start, end, api = globalThis.wx } = {}) {
  if (!quietHoursNeedsLengthConfirm(start, end)) {
    return Promise.resolve(true);
  }
  const duration = quietHoursDurationMinutes(start, end);
  const span = formatDurationForConfirm(duration);
  return new Promise((resolve) => {
    if (!api || typeof api.showModal !== "function") {
      resolve(false);
      return;
    }
    api.showModal({
      title: "确认夜间模式时段",
      content: `当前夜间模式为 ${start}${EN_DASH}${end}，约 ${span}。少于 1 小时或超过 12 小时时请再确认一下。仍要使用这个时段吗？`,
      confirmText: "仍要使用",
      cancelText: "再改改",
      success: (res) => resolve(Boolean(res && res.confirm)),
      fail: () => resolve(false),
    });
  });
}

module.exports = {
  DEFAULT_QUIET_HOURS,
  MIN_USUAL_MINUTES,
  MAX_USUAL_MINUTES,
  quietHoursDurationMinutes,
  quietHoursNeedsLengthConfirm,
  nightModeQuietHoursLabel,
  confirmUnusualQuietHours,
};
