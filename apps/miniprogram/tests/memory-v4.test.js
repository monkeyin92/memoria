const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const NO_ALERTS = fx.safetyAlertApi([]);

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openMemory(overrides = NO_ALERTS) {
  const harness = loadPage("pages/memory/index", fx.authed(overrides));
  await harness.run();
  return harness;
}

test("recent summaries come first and count-only days collapse into one card", async () => {
  const harness = await openMemory();
  try {
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /^最近几天 对话回顾 这里只有摘要，不显示原话，也没有录音。 今天 10月14日 /);
    assert.match(text, /更多日期 4 天 还有 4 天有聊天记录，共 8 次/);
    assert.equal((text.match(/孩子聊了 \d+ 次/g) || []).length, 0);
    assert.doesNotMatch(text, /三 14 二 13 一 12 日 11 六 10 五 9/);
    assert.doesNotMatch(text, /日常回顾|· 孩子聊了/);
  } finally {
    harness.restore();
  }
});

test("memory collapses a count-only guardian response into one summary card", async () => {
  const harness = await openMemory({
    ...NO_ALERTS,
    getGuardianChildDays: async () => ({ items: [{ day: fx.dayKey(0), message_count: 1 }] }),
    getChildSessionSummaries: async () => ({ items: [] }),
  });
  try {
    assert.equal(harness.page.data.dateStrip, undefined);
    assert.equal(harness.page.data.selectedDate, undefined);
    assert.equal(harness.page.chooseDay, undefined);
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.doesNotMatch(text, /三 14 二 13 一 12 日 11 六 10 五 9/);
    assert.match(text, /最近 30 天 1 天 还有 1 天有聊天记录，共 1 次/);
    assert.equal((text.match(/孩子聊了 \d+ 次/g) || []).length, 0);
  } finally {
    harness.restore();
  }
});

test("unknown guardian message counts stay unknown instead of becoming zero", async () => {
  const harness = await openMemory({
    ...NO_ALERTS,
    getGuardianChildDays: async () => ({
      items: [
        { day: fx.dayKey(0), message_count: 3 },
        { day: fx.dayKey(1) },
      ],
    }),
    getChildSessionSummaries: async () => ({ items: [] }),
  });
  try {
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /还有 2 天有聊天记录，共 3 次，另有 1 天次数待同步/);
    assert.doesNotMatch(text, /共 3 次，另有 1 天次数待同步。.*0 次/);
    assert.doesNotMatch(text, /共 0 次/);
  } finally {
    harness.restore();
  }
});

test("the empty review state opens device settings and has no voice-tab copy", async () => {
  const harness = await openMemory({
    ...NO_ALERTS,
    getGuardianChildDays: async () => ({ items: [] }),
    getChildSessionSummaries: async () => ({ items: [] }),
  });
  try {
    const text = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /去设备设置/);
    assert.doesNotMatch(text, /设置声音与表达/);
    assert.equal(harness.page.goCompanion, undefined);
    harness.page.goDeviceSettings();
    assert.deepEqual(
      harness.wxCalls.filter((call) => call.name === "switchTab").map((call) => call.args[0].url),
      ["/pages/device/index"],
    );
  } finally {
    harness.restore();
  }
});

test("记住的事 opens from a row under the days and 日常回顾 goes back", async () => {
  const harness = await openMemory();
  try {
    const reviews = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(reviews, /记住的事 确认过的事，机器人才会在以后的聊天里提起 AI 整理可能有偏差/);
    assert.doesNotMatch(reviews, /分享这一天|整理选中这一天|生成摘要/);

    harness.page.showMemories();
    assert.equal(harness.page.data.memoryTab, "memories");
    const memories = visibleText(renderPage("pages/memory/index", harness.page.data, { tabBar: false }));
    assert.match(memories, /对话回顾 这里只有摘要，不显示原话，也没有录音。 日常回顾 记住的事 这里是孩子和机器人聊天时可能被记住的事。/);
    assert.doesNotMatch(memories, /整理选中这一天/);

    harness.page.showReviews();
    assert.equal(harness.page.data.memoryTab, "reviews");
  } finally {
    harness.restore();
  }
});

test("cold binding cache syncs before guardian review and safety reads", async () => {
  const calls = [];
  const binding = fx.authed().api.readBindingManifest();
  let cachedBinding = null;
  const h = await openMemory({
    readBindingManifest: () => cachedBinding,
    syncDeviceBindings: async () => { cachedBinding = binding; return { status: "ready", binding }; },
    getMemoryDays: async () => { calls.push("adult"); return { items: [] }; },
    getConversationReview: async () => { calls.push("adult-review"); return {}; },
    getUnreadGuardianNotifications: async () => { calls.push("safety"); return { newest: fx.SAFETY_ALERT, others_count: 0 }; },
  });
  try {
    assert.equal(h.page.data.guardianMode, true);
    assert.deepEqual(calls, ["safety"]);
    assert.match(visibleText(renderPage("pages/memory/index", h.page.data)), /有一条需要你关注的对话/);
  } finally { h.restore(); }
});

for (const status of ["error", "choose", "cached"]) {
  test(`unconfirmed binding ${status} never loads adult data`, async () => {
    let adultReads = 0;
    const h = await openMemory({
      readBindingManifest: () => null,
      syncDeviceBindings: async () => ({ status, binding: null }),
      getMemoryDays: async () => { adultReads++; return { items: [] }; },
      getConversationReview: async () => { adultReads++; return {}; },
    });
    try {
      assert.equal(adultReads, 0);
      assert.equal(h.page.data.guardianMode, true);
      assert.match(visibleText(renderPage("pages/memory/index", h.page.data)), /无法确认设备绑定/);
      assert.equal(h.page.data.safetyReadStatus, "failed");
    } finally { h.restore(); }
  });
}

test("confirmed empty bindings retain adult review", async () => {
  let adultReads = 0;
  const h = await openMemory({
    readBindingManifest: () => null,
    syncDeviceBindings: async () => ({ status: "empty", binding: null }),
    getConversationReview: async () => { adultReads++; return {}; },
  });
  try {
    assert.equal(adultReads, 1);
    assert.equal(h.page.data.guardianMode, false);
  } finally { h.restore(); }
});

test("a confirmed child binding without its child never opens adult review", async () => {
  let adultReads = 0;
  const binding = { ...fx.authed().api.readBindingManifest(), primary_subject_ids: [] };
  let cachedBinding = null;
  const h = await openMemory({
    readBindingManifest: () => cachedBinding,
    syncDeviceBindings: async () => { cachedBinding = binding; return { status: "ready", binding }; },
    getConversationReview: async () => { adultReads++; return {}; },
  });
  try {
    assert.equal(adultReads, 0);
    assert.match(visibleText(renderPage("pages/memory/index", h.page.data)), /无法确认设备绑定/);
  } finally { h.restore(); }
});

test("retrying a failed cold binding read restores guardian safety and review", async () => {
  let available = false;
  const binding = fx.authed().api.readBindingManifest();
  let cachedBinding = null;
  const h = await openMemory({
    readBindingManifest: () => cachedBinding,
    syncDeviceBindings: async () => {
      if (!available) return { status: "error", binding: null };
      cachedBinding = binding;
      return { status: "ready", binding };
    },
    ...fx.safetyAlertApi([fx.SAFETY_ALERT]),
  });
  try {
    assert.match(visibleText(renderPage("pages/memory/index", h.page.data)), /暂时没能读取，点击重试/);
    available = true;
    await h.page.retrySafetyRead();
    assert.equal(h.page.data.safetyReadStatus, "ok");
    assert.match(visibleText(renderPage("pages/memory/index", h.page.data)), /有一条需要你关注的对话/);
  } finally { h.restore(); }
});
