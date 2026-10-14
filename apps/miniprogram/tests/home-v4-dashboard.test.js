const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");
const { alertTimeLabel } = require("../utils/trial-device-card");

async function loadHome(options = {}) {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(options.api), now: fx.NOW, config: options.config });
  try {
    const data = await harness.run();
    return { data, setDataLog: harness.setDataLog };
  } finally {
    harness.restore();
  }
}

function visibleText(html) {
  return html.replace(/<style>[\s\S]*?<\/style>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

test("home ring shows the daily limit without inventing used minutes", async () => {
  const { data } = await loadHome();
  assert.equal(data.usageKnown, false);
  assert.equal(data.usageUsedMinutes, null);
  assert.equal(data.usagePercent, 0);
  assert.equal(data.usageLimitMinutes, 30);
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /30 分钟上限/);
  assert.match(text, /用量暂未统计/);
  assert.doesNotMatch(text, /\d+%/);
});

test("home ring fills from a real used-minutes tally when one exists", async () => {
  const { data } = await loadHome();
  const html = renderPage("pages/home/index", { ...data, usageKnown: true, usageUsedMinutes: 18, usagePercent: 60 });
  const text = visibleText(html);
  assert.match(text, /60% 上限 30 分钟/);
  assert.match(text, /今天聊了 18 分钟/);
  assert.match(html, /conic-gradient\(var\(--mp-accent\) 0 60%, var\(--mp-track\) 60% 100%\)/);
});

test("home counts only today's summaries and today's conversations", async () => {
  const { data } = await loadHome();
  assert.equal(data.summaryCount, 3);
  assert.equal(data.todayCount, 3);
  assert.deepEqual(
    data.memoryPreviews.map((item) => item.text),
    ["讲了《大熊和森林里的悄悄话》", "聊到了幼儿园的新朋友", "一起数了天上的星星"],
  );
  const empty = await loadHome({ api: { getChildSessionSummaries: async () => ({ items: [] }), getMemoryDays: async () => ({ items: [] }) } });
  assert.equal(empty.data.summaryCount, 0);
  assert.equal(empty.data.todayCount, 0);
});

test("home pins the attention card with its time and the open-to-pin meta", async () => {
  const { data } = await loadHome();
  assert.equal(data.unreadSafetyAlert, true);
  assert.equal(data.unreadSafetyCopy, "有一条需要你关注的对话");
  assert.equal(data.unreadSafetyMeta, "今天 19:42 · 打开时首页会置顶显示");
  const body = renderPage("pages/home/index", data).replace(/<style>[\s\S]*?<\/style>/, "");
  assert.ok(body.indexOf("有一条需要你关注的对话") < body.indexOf("home-usage-card"), "attention card renders above the usage ring");
  const calm = await loadHome({ api: { getGuardianNotifications: async () => ({ items: [] }) } });
  assert.equal(calm.data.unreadSafetyAlert, false);
  assert.doesNotMatch(renderPage("pages/home/index", calm.data), /需要你关注/);
});

test("home hides the safety subscribe switch unless subscription push is enabled", async () => {
  const off = await loadHome();
  assert.equal(off.data.subscriptionPushEnabled, false);
  const offText = visibleText(renderPage("pages/home/index", off.data));
  assert.doesNotMatch(offText, /安全提醒/);
  assert.match(offText, /安静时段 21:00 – 次日 07:00/);

  const on = await loadHome({ config: { CONTROL_API_BASE_URL: "https://example.invalid", SUBSCRIPTION_PUSH_ENABLED: true } });
  assert.equal(on.data.subscriptionPushEnabled, true);
  assert.match(visibleText(renderPage("pages/home/index", on.data)), /安全提醒/);
});

test("home never renders battery, story counts or a recommendation card", async () => {
  const { data } = await loadHome();
  const text = visibleText(renderPage("pages/home/index", data));
  assert.doesNotMatch(text, /电量|电池|个故事|今晚推荐/);
});

test("guest home shows the v4 login hero", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  const data = await harness.run();
  harness.restore();
  assert.equal(data.authenticated, false);
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /Memoria 陪孩子聊天、讲故事的 AI 助手/);
  assert.match(text, /微信一键登录/);
});

test("alert time label reads today or the calendar day", () => {
  const now = new Date("2026-10-14T21:00:00+08:00");
  assert.equal(alertTimeLabel("2026-10-14T19:42:00+08:00", now), "今天 19:42");
  assert.equal(alertTimeLabel("2026-10-12T08:05:00+08:00", now), "10月12日 08:05");
  assert.equal(alertTimeLabel("not a date", now), "");
});

test("home load stays within the setData budget", async () => {
  const { setDataLog } = await loadHome();
  const total = setDataLog.reduce((sum, entry) => sum + entry.bytes, 0);
  const largest = Math.max(...setDataLog.map((entry) => entry.bytes));
  assert.ok(setDataLog.length <= 6, `setData calls ${setDataLog.length}`);
  assert.ok(largest < 8 * 1024, `largest setData ${largest} bytes`);
  assert.ok(total < 16 * 1024, `total setData ${total} bytes`);
});
