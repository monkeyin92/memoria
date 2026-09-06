function greetingFor(date = new Date()) {
  const hour = date.getHours();
  if (hour < 6 || hour >= 22) return "夜深了";
  if (hour < 12) return "早上好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

function formatDateLabel(date = new Date()) {
  const week = "日一二三四五六"[date.getDay()];
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日，星期${week}`;
}

module.exports = { greetingFor, formatDateLabel };
