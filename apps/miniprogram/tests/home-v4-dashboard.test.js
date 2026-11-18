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

test("home leaves the usage bar hidden when the ledger could not read today's minutes", async () => {
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
  assert.match(text, /今日聊天用量 用量暂未统计 上限 30 分钟/);
  assert.doesNotMatch(text, /\d+%|单次上限/);
  assert.doesNotMatch(html, /class="progress-fill"/);
});

test("home shows today's used minutes and daily cap", async () => {
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
  assert.match(text, /18 \/ 30 分钟 上限 30 分钟/);
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
  assert.match(html, /class="progress-fill"[^>]*style="width: 13%;/);
  assert.match(visibleText(html), /4 \/ 30 分钟 上限 30 分钟/);
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
  assert.match(text, /18 \/ 30 分钟/);
  assert.doesNotMatch(text, /用量暂未统计/);
  assert.equal(data.usageFillPercent, 60);
  assert.match(html, /class="progress-fill"[^>]*style="width: 60%;/);

  const failed = await loadHome({
    api: {
      getSessionLimits: async () => {
        throw new Error("session_limits_unavailable");
      },
    },
  });
  assert.equal(failed.data.usageKnown, false);
  assert.match(visibleText(renderPage("pages/home/index", failed.data)), /用量暂未统计/);
});

test("home links to device settings when the signed profile has no daily budget", async () => {
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
  assert.match(text, /查看设备设置/);
  assert.match(text, /18 分钟/);
  assert.match(text, /安静时段 未设置/);
  assert.doesNotMatch(text, /21:00|\b30\b/);
});

test("home can show an account review without a child archive", async () => {
  const { data } = await loadHome();
  const html = renderPage("pages/home/index", {
    ...data,
    memoryPreviews: [],
    showChildMemory: false,
    dailySummaryText: "今天和 Memoria 聊了 2 次。",
    todayCount: 2,
  });
  const text = visibleText(html);
  assert.match(text, /今天和 Memoria 聊了 2 次。/);
  assert.doesNotMatch(text, /还没有摘要|近期摘要 0 条/);
});

test("home counts all recent summaries and today's conversations separately", async () => {
  const { data } = await loadHome();
  assert.equal(data.summaryCount, 4);
  assert.equal(data.todayCount, 3);
  assert.equal(data.todayCountKnown, true);
  assert.deepEqual(
    data.memoryPreviews.map((item) => item.text),
    ["讲了《大熊和森林里的悄悄话》", "聊到了幼儿园的新朋友", "一起数了天上的星星"],
  );
  const homeText = visibleText(renderPage("pages/home/index", data));
  assert.match(homeText, /近期回顾/);
  assert.match(homeText, /今天对话 3 次 近期摘要 4 条/);
  const empty = await loadHome({
    api: {
      getChildSessionSummaries: async () => ({ items: [] }),
      getGuardianChildDays: async () => ({ items: [] }),
    },
  });
  assert.equal(empty.data.summaryCount, 0);
  assert.equal(empty.data.todayCount, 0);
  assert.equal(empty.data.todayCountKnown, true);
});

test("home pins the attention card with its time and the open-to-pin meta", async () => {
  const { data } = await loadHome();
  assert.equal(data.unreadSafetyAlert, true);
  assert.equal(data.unreadSafetyCopy, "有一条需要你关注的对话");
  assert.equal(data.unreadSafetyMeta, "今天 19:42 · 打开时首页会置顶显示");
  const body = renderPage("pages/home/index", data).replace(/<style>[\s\S]*?<\/style>/, "");
  assert.ok(body.indexOf("有一条需要你关注的对话") < body.indexOf("home-review-card"), "attention card renders above the review card");
  const calm = await loadHome({ api: fx.safetyAlertApi([]) });
  assert.equal(calm.data.unreadSafetyAlert, false);
  assert.doesNotMatch(renderPage("pages/home/index", calm.data), /需要你关注/);
});

test("home shows the safety subscribe card only while subscription push is enabled", async () => {
  // The card hides only when the toggle is off; the shipped default is on (#029).
  const off = await loadHome({ config: { CONTROL_API_BASE_URL: "https://example.invalid", SUBSCRIPTION_PUSH_ENABLED: false } });
  assert.equal(off.data.subscriptionPushEnabled, false);
  const offText = visibleText(renderPage("pages/home/index", off.data));
  assert.doesNotMatch(offText, /安全提醒|安静时段/);

  const on = await loadHome();
  assert.equal(on.data.subscriptionPushEnabled, true);
  const onText = visibleText(renderPage("pages/home/index", on.data));
  assert.match(onText, /安全提醒/);
  assert.match(onText, /安静时段 21:00 – 次日 07:00/);
});

test("home says the last safety alert used up the subscribe and keeps the switch off", async () => {
  const spent = await loadHome({
    api: {
      getGuardianPushConfig: async () => ({ enabled: true, template_ids: { crisis: "tmpl-crisis" } }),
      getGuardianPushSubscription: async () => ({
        template_id: "tmpl-crisis",
        remaining: 0,
        subscribed: false,
        last_result: "accept",
      }),
    },
  });
  assert.equal(spent.data.safetySubscribeUsedUp, true);
  assert.equal(spent.data.safetySubscribeOn, false);
  const spentText = visibleText(renderPage("pages/home/index", spent.data));
  assert.match(spentText, /上一次提醒已经发出。再开启一次，下次才能继续提醒你。/);
  assert.doesNotMatch(spentText, /家长打开小程序时首页会置顶显示/);

  const remaining = await loadHome({
    api: {
      getGuardianPushConfig: async () => ({ enabled: true, template_ids: { crisis: "tmpl-crisis" } }),
      getGuardianPushSubscription: async () => ({
        template_id: "tmpl-crisis",
        remaining: 1,
        subscribed: true,
        last_result: "accept",
      }),
    },
  });
  assert.equal(remaining.data.safetySubscribeUsedUp, false);
  assert.equal(remaining.data.safetySubscribeOn, true);
  const remainingText = visibleText(renderPage("pages/home/index", remaining.data));
  assert.match(remainingText, /家长打开小程序时首页会置顶显示/);
  assert.doesNotMatch(remainingText, /上一次提醒已经发出/);
});

function answeringSubscribeWx() {
  const harnessWx = global.wx;
  const counter = { prompts: 0 };
  global.wx = new Proxy(harnessWx, {
    get(target, name) {
      if (name === "requestSubscribeMessage") {
        return (options) => {
          counter.prompts += 1;
          const templateId = options.tmplIds?.[0];
          options.success({ errMsg: "requestSubscribeMessage:ok", [templateId]: "accept" });
        };
      }
      return target[name];
    },
  });
  return {
    counter,
    restore() {
      global.wx = harnessWx;
    },
  };
}

test("tapping the home safety switch opens the WeChat prompt inside the tap and records the answer", async () => {
  const recorded = [];
  const harness = loadPage("pages/home/index", {
    api: fx.baseApi({
      getGuardianPushConfig: async () => ({ enabled: true, template_ids: { crisis: "tmpl-crisis" } }),
      getGuardianPushSubscription: async () => ({
        template_id: "tmpl-crisis",
        remaining: 0,
        subscribed: false,
        last_result: "accept",
      }),
      wechatLoginCode: async () => "login-code-home",
      recordGuardianPushSubscription: async (body) => {
        recorded.push(body);
        return { template_id: body.templateId, result: body.result, remaining: 1 };
      },
    }),
    now: fx.NOW,
  });
  const wx = answeringSubscribeWx();
  try {
    await harness.run();
    const page = harness.page;
    assert.equal(page.data.safetySubscribeKnown, true);
    assert.equal(page.data.safetySubscribeUsedUp, true);
    const pending = page.onSafetySubscribeTap();
    assert.equal(wx.counter.prompts, 1);
    await pending;
    assert.deepEqual(recorded, [{ templateId: "tmpl-crisis", result: "accept", loginCode: "login-code-home" }]);
    assert.equal(page.data.safetySubscribeOn, true);
    assert.equal(page.data.safetySubscribeUsedUp, false);
    const text = visibleText(renderPage("pages/home/index", page.data));
    assert.doesNotMatch(text, /上一次提醒已经发出/);
  } finally {
    wx.restore();
    harness.restore();
  }
});

test("home links to child memory without repeating the review preview", async () => {
  const { data } = await loadHome();
  const text = visibleText(renderPage("pages/home/index", data));
  assert.match(text, /孩子的记忆 查看长期记忆与近期摘要/);
  assert.match(renderPage("pages/home/index", data), /class="trial-card home-row-card home-memory-entry" data-tap/);
});

test("home never renders battery, story counts or a recommendation card", async () => {
  const { data } = await loadHome();
  const text = visibleText(renderPage("pages/home/index", data));
  assert.doesNotMatch(text, /电量|电池|个故事|今晚推荐/);
});

test("guest home lists the product intro and stays off the login screen", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  try {
    const data = await harness.run();
    assert.equal(data.authenticated, false);
    assert.deepEqual(harness.tabBar.data, { selected: 0, hidden: true });
    const text = visibleText(renderPage("pages/home/index", data, { tabBar: false }));
    assert.match(text, /Memoria 是给 3 到 10 岁孩子的陪伴机器人/);
    assert.match(text, /陪聊 离线故事 每次和每天的时长上限 家长能查看和删除聊天摘要/);
    assert.doesNotMatch(text, /服务器不存孩子的录音|导出|微信一键登录|登录即表示|选择头像/);
    assert.equal(harness.wxCalls.filter((call) => call.name === "login").length, 0);
    assert.equal(harness.wxCalls.filter((call) => call.name === "request").length, 0);
  } finally {
    harness.restore();
  }
});

test("guest login opens the login page without wx.login", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  try {
    await harness.run();
    global.getCurrentPages = () => [{ route: "pages/home/index", options: {} }];
    harness.page.openAccountLogin();
    const navigations = harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url);
    assert.deepEqual(navigations, ["/pages/auth/index?redirect=%2Fpages%2Fhome%2Findex&reason=view_dashboard"]);
    assert.equal(harness.wxCalls.filter((call) => call.name === "login").length, 0);
  } finally {
    harness.restore();
  }
});

test("a guest gated action shows the login modal and does not obtain an identity", async () => {
  const harness = loadPage("pages/home/index", { api: fx.baseApi(fx.GUEST_API), now: fx.NOW });
  try {
    await harness.run();
    global.wx.showModal = (options) => {
      assert.equal(options.content, "这个功能需要先登录，登录后才能绑定和管理你的设备");
      options.success?.({ confirm: true });
    };
    await harness.page.onGuestGatedAction({ currentTarget: { dataset: { id: "bind_device" } } });
    const navigations = harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url);
    assert.equal(navigations.length, 1);
    assert.match(navigations[0], /\/pages\/auth\/index\?/);
    assert.match(navigations[0], /reason=bind_device/);
    assert.equal(harness.wxCalls.filter((call) => call.name === "login").length, 0);
    assert.equal(harness.page.data.authenticated, false);
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
