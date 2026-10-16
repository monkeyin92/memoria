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
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
}

test("home ring shows the daily budget without inventing minutes the ledger could not read", async () => {
  const { data } = await loadHome({
    api: {
      getSessionLimits: async () => ({
        max_session_minutes: 30,
        quiet_hours: null,
        daily_chat_minutes: 30,
        daily_chat_minute_choices: [15, 30, 45, 60],
        today_used_minutes: null,
      }),
    },
  });
  assert.equal(data.usageKnown, false);
  assert.equal(data.usageUsedMinutes, null);
  assert.equal(data.usageLimitMinutes, 30);
  const html = renderPage("pages/home/index", data);
  const text = visibleText(html);
  assert.match(text, /30 分钟 · 每天上限 今天聊了 用量暂未统计/);
  assert.doesNotMatch(text, /\d+%|单次上限/);
  assert.match(html, /conic-gradient\(#4A52C9 0% 0%, #ECECF2 0% 100%\)/);
});

test("the home ring center reads the used percent and the daily cap", async () => {
  const { data } = await loadHome({
    api: {
      getSessionLimits: async () => ({
        max_session_minutes: 15,
        quiet_hours: null,
        daily_chat_minutes: 30,
        today_used_minutes: 18,
      }),
    },
  });
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /60% 上限 30 分钟 今天聊了 18 分钟/);
});

test("home ring fills the used share of the daily budget", async () => {
  const { data } = await loadHome({
    api: {
      getSessionLimits: async () => ({
        max_session_minutes: 15,
        quiet_hours: null,
        daily_chat_minutes: 30,
        today_used_minutes: 4,
      }),
    },
  });
  assert.equal(data.usageUsedMinutes, 4);
  assert.equal(data.usageLimitMinutes, 30);
  assert.equal(data.usageFillPercent, 13);
  const html = renderPage("pages/home/index", data);
  assert.match(html, /conic-gradient\(#4A52C9 0% 13%, #ECECF2 13% 100%\)/);
  assert.match(visibleText(html), /今天聊了 4 分钟/);
});

test("home shows today's minutes only when the session-limits read carries them", async () => {
  const reads = [];
  const { data } = await loadHome({
    api: {
      getSessionLimits: async (deviceId) => {
        reads.push(deviceId);
        return { max_session_minutes: 30, quiet_hours: null, today_used_minutes: 18 };
      },
    },
  });
  assert.deepEqual(reads, ["dev_1"]);
  assert.equal(data.usageKnown, true);
  assert.equal(data.usageUsedMinutes, 18);
  const html = renderPage("pages/home/index", data);
  const text = visibleText(html);
  assert.match(text, /30 分钟 · 每天上限 今天聊了 18 分钟/);
  assert.doesNotMatch(text, /用量暂未统计|\d+%/);
  assert.equal(data.usageFillPercent, 60);
  assert.match(html, /conic-gradient\(#4A52C9 0% 60%, #ECECF2 60% 100%\)/);

  const failed = await loadHome({
    api: {
      getSessionLimits: async () => {
        throw new Error("session_limits_unavailable");
      },
    },
  });
  assert.equal(failed.data.usageKnown, false);
  assert.match(visibleText(renderPage("pages/home/index", failed.data)), /今天聊了 用量暂未统计/);
});

test("home ring reads 未设置 when the signed profile carries a session cap but no daily budget", async () => {
  const { data } = await loadHome({
    api: {
      getRuntimeProfile: async () => ({
        ...fx.RUNTIME_PROFILE,
        obligations: [{ code: "MAX_SESSION_SECONDS", params: { max_session_seconds: 1800, extras: [] } }],
      }),
    },
    config: { CONTROL_API_BASE_URL: "https://example.invalid", SUBSCRIPTION_PUSH_ENABLED: true },
  });
  assert.equal(data.usageLimitMinutes, null);
  assert.equal(data.quietHoursLabel, "");
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /未设置 每天上限 今天聊了 18 分钟/);
  assert.match(text, /安静时段 未设置/);
  assert.doesNotMatch(text, /21:00|\b30\b/);
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

test("home hides the safety subscribe card unless subscription push is enabled", async () => {
  const off = await loadHome();
  assert.equal(off.data.subscriptionPushEnabled, false);
  const offText = visibleText(renderPage("pages/home/index", off.data));
  assert.doesNotMatch(offText, /安全提醒|安静时段/);

  const on = await loadHome({ config: { CONTROL_API_BASE_URL: "https://example.invalid", SUBSCRIPTION_PUSH_ENABLED: true } });
  assert.equal(on.data.subscriptionPushEnabled, true);
  const onText = visibleText(renderPage("pages/home/index", on.data));
  assert.match(onText, /安全提醒/);
  assert.match(onText, /安静时段 21:00 – 次日 07:00/);
});

test("home memory card previews the latest summary and opens child memory", async () => {
  const { data } = await loadHome();
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /孩子的记忆 最近：讲了《大熊和森林里的悄悄话》/);
  assert.match(renderPage("pages/home/index", data), /class="trial-card home-row-card" data-tap/);
});

test("home never renders battery, story counts or a recommendation card", async () => {
  const { data } = await loadHome();
  const text = visibleText(renderPage("pages/home/index", data));
  assert.doesNotMatch(text, /电量|电池|个故事|今晚推荐/);
});

test("guest home is the full-bleed v4 login screen with no tab bar", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  try {
    const data = await harness.run();
    assert.equal(data.authenticated, false);
    assert.deepEqual(harness.tabBar.data, { selected: 0, hidden: true });
    const text = visibleText(renderPage("pages/home/index", data, { tabBar: false }));
    assert.equal(
      text.trim(),
      "Memoria 陪孩子聊天、讲故事的 AI 助手 晚上好，欢迎回来 登录后查看孩子今天聊了什么 微信一键登录 手机号登录 登录即表示你已阅读《用户协议》和《隐私说明》",
    );
  } finally {
    harness.restore();
  }
});

test("guest 手机号登录 opens the phone authorization page and returns home", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  try {
    await harness.run();
    global.getCurrentPages = () => [{ route: "pages/home/index", options: {} }];
    harness.page.loginWithPhone();
    const navigations = harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url);
    assert.deepEqual(navigations, ["/pages/auth/index?redirect=%2Fpages%2Fhome%2Findex&reason=view_dashboard"]);
  } finally {
    harness.restore();
  }
});

test("the tab bar returns once the guest signs in from home", async () => {
  let signedIn = false;
  const api = fx.baseApi({
    hasAuthenticatedSession: () => signedIn,
    restoreWechatIdentity: async () => {
      signedIn = true;
    },
  });
  const harness = loadPage("pages/home/index", { api, now: fx.NOW });
  try {
    await harness.run();
    assert.equal(harness.tabBar.data.hidden, true);
    await harness.page.loginFromHome();
    await harness.settle();
    assert.equal(harness.page.data.authenticated, true);
    assert.deepEqual(harness.tabBar.data, { selected: 0, hidden: false });
  } finally {
    harness.restore();
  }
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
