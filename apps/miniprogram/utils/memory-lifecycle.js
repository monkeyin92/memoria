const ISO_WITH_ZONE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?(Z|([+-])(\d{2}):(\d{2}))$/;

function twoDigits(value) {
  return String(value).padStart(2, "0");
}

/*
 * Backend times are ISO 8601 with Z or an explicit offset. Build the UTC
 * instant from the parts ourselves: iOS rejects the space-separated
 * "2026-10-19 08:00:00" form, so a string that carries no zone is not a date
 * here at all. The display is always UTC+8 (Beijing), independent of the
 * phone's timezone.
 */
function formatBeijingTime(value) {
  if (typeof value !== "string" || !value) return "";
  const match = ISO_WITH_ZONE.exec(value.trim());
  if (!match) return "";

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = match[6] === undefined ? 0 : Number(match[6]);
  const millisecond = match[7] === undefined ? 0 : Number(match[7].padEnd(3, "0"));
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31 ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return "";
  }

  let offsetMinutes = 0;
  if (match[8] !== "Z") {
    const offsetHour = Number(match[10]);
    const offsetMinute = Number(match[11]);
    if (offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)) {
      return "";
    }
    offsetMinutes = (offsetHour * 60 + offsetMinute) * (match[9] === "-" ? -1 : 1);
  }

  const parsed = new Date(0);
  parsed.setUTCFullYear(year, month - 1, day);
  parsed.setUTCHours(hour, minute, second, millisecond);
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day ||
    parsed.getUTCHours() !== hour ||
    parsed.getUTCMinutes() !== minute ||
    parsed.getUTCSeconds() !== second ||
    parsed.getUTCMilliseconds() !== millisecond
  ) {
    return "";
  }

  const beijing = new Date(parsed.getTime() - offsetMinutes * 60000 + 8 * 3600000);
  const stamp = beijing.getTime();
  if (!Number.isFinite(stamp)) return "";
  return `${beijing.getUTCMonth() + 1}月${beijing.getUTCDate()}日 ${twoDigits(
    beijing.getUTCHours(),
  )}:${twoDigits(beijing.getUTCMinutes())}`;
}

module.exports = {
  formatBeijingTime,
};
