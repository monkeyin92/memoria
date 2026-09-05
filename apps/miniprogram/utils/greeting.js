function greetingFor(date = new Date()) {
  const hour = date.getHours();
  if (hour < 6 || hour >= 22) return "夜深了";
  if (hour < 12) return "早上好";
  if (hour < 18) return "下午好";
  return "晚上好";
}

module.exports = { greetingFor };
