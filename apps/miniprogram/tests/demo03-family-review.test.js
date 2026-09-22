const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");

const root = path.join(__dirname, "..");
const storage = {};
let pageDefinition = null;

function withWx(fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showToast() {},
    stopPullDownRefresh() {},
    navigateTo() {},
    showModal(options) {
      options.success?.({ confirm: true });
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner", display_name: "主人" },
      accessToken: "t",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
    subscribeAuthCleared: () => () => {},
  });
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
    return fn();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
}

function loadPage(relativePath) {
  const resolved = require.resolve(relativePath);
  delete require.cache[resolved];
  require(resolved);
  return pageDefinition;
}

function instantiate(definition) {
  const instance = { ...definition };
  instance.data = JSON.parse(JSON.stringify(definition.data));
  instance.setData = (updates) => {
    for (const [key, value] of Object.entries(updates)) instance.data[key] = value;
  };
  return instance;
}

function stubApi(overrides) {
  const originals = {};
  for (const [key, value] of Object.entries(overrides)) {
    originals[key] = api[key];
    api[key] = value;
  }
  return () => {
    for (const [key, value] of Object.entries(originals)) {
      if (value === undefined) delete api[key];
      else api[key] = value;
    }
  };
}

function allowedGate() {
  return { allowed: true, reason: "allowed", profile: { valid: true } };
}

function todayKey(date = new Date()) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

test("home today count uses gated session reads and composes the daily summary", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const restore = stubApi({
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({
        items: [
          {
            day: todayKey(),
            message_count: 9,
            summary: { title: "今天聊了考试", overview: "他提到数学考了 95 分。" },
          },
        ],
      }),
      getConversationReview: async () => ({ memory_candidates: [] }),
      getConversationSessions: async () => ({
        items: [
          { session_id: "s-today", occurred_at: new Date().toISOString(), turn_count: 3 },
          {
            session_id: "s-old",
            occurred_at: "2020-01-01T00:00:00Z",
            turn_count: 5,
          },
        ],
      }),
    });
    try {
      const result = await page._loadToday({ user_id: "person_owner" });
      assert.equal(result.todayCount, 1, "只统计今天的会话");
      assert.equal(result.todayMeta, "今天 · 1 次对话");
      assert.equal(
        result.dailySummaryText,
        "今天和 Memoria 聊了 1 次。他提到数学考了 95 分。",
      );
      assert.equal(result.pendingCount, 0);
    } finally {
      restore();
    }
  });
});

test("home falls back to the day count when the session list is unavailable", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const restore = stubApi({
      requireRuntimeCapability: async () => allowedGate(),
      getMemoryDays: async () => ({ items: [{ day: todayKey(), message_count: 3 }] }),
      getConversationReview: async () => ({ memory_candidates: [] }),
      getConversationSessions: async () => {
        throw new Error("session index unavailable");
      },
    });
    try {
      const result = await page._loadToday({ user_id: "person_owner" });
      assert.equal(result.todayCount, 3);
      assert.equal(result.todayMeta, "今天 · 3 次对话");
      assert.equal(
        result.dailySummaryText,
        "今天和 Memoria 聊了 3 次。回顾还没生成，可在回顾页生成。",
        "没有服务端回顾时不得编造摘要内容",
      );
    } finally {
      restore();
    }
  });
});

test("home refuses private reads without the capability", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    let sessionCalls = 0;
    const restore = stubApi({
      requireRuntimeCapability: async () => ({ allowed: false, reason: "capability_missing" }),
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
      getConversationSessions: async () => {
        sessionCalls += 1;
        return { items: [] };
      },
    });
    try {
      const result = await page._loadToday({ user_id: "person_owner" });
      assert.equal(memoryCalls, 0);
      assert.equal(sessionCalls, 0);
      assert.equal(result.todayCount, 0);
      assert.equal(result.dailySummaryText, "");
    } finally {
      restore();
    }
  });
});

test("home share carries the gated summary only when authenticated", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    page.setData({
      authenticated: true,
      dailySummaryText: "今天和 Memoria 聊了 2 次。他提到数学考了 95 分。",
    });
    const shared = page.onShareAppMessage();
    assert.equal(shared.path, "/pages/home/index");
    assert.equal(shared.title, "今天和 Memoria 聊了 2 次。他提到数学考了 95 分。");

    page.setData({ authenticated: false });
    const guest = page.onShareAppMessage();
    assert.equal(guest.path, "/pages/home/index");
    assert.doesNotMatch(guest.title, /数学|聊了/, "未登录分享不得带出私人摘要");
    assert.match(guest.title, /Memoria/);
  });
});

test("memory share uses the pressed day and falls back for guests", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/memory/index");
    const page = instantiate(definition);
    page.setData({
      authenticated: true,
      selectedDate: "2026-09-22",
      days: [
        { date: "2026-09-22", title: "今天的回顾", overview: "聊了数学考试。" },
        { date: "2026-09-20", title: "昨天的回顾", overview: "聊了恐龙。" },
      ],
    });
    const pressed = page.onShareAppMessage({
      from: "button",
      target: { dataset: { date: "2026-09-20" } },
    });
    assert.equal(pressed.path, "/pages/memory/index");
    assert.equal(pressed.title, "Memoria 回顾 · 昨天的回顾");

    const menu = page.onShareAppMessage({ from: "menu" });
    assert.equal(menu.title, "Memoria 回顾 · 今天的回顾");

    page.setData({ authenticated: false });
    const guest = page.onShareAppMessage({ from: "menu" });
    assert.equal(guest.title, "Memoria · 每天的对话回顾");
    assert.doesNotMatch(guest.title, /昨天的回顾|恐龙/);
  });
});

test("DEMO-03 templates expose the daily summary card, share buttons and previews", () => {
  const home = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const homeScript = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  const memory = fs.readFileSync(path.join(root, "pages/memory/index.wxml"), "utf8");
  const memoryScript = fs.readFileSync(path.join(root, "pages/memory/index.js"), "utf8");

  assert.match(home, /每日摘要/);
  assert.match(home, /open-type="share"/);
  assert.match(home, /\{\{dailySummaryText \|\| todayOverview\}\}/);
  assert.match(homeScript, /次对话/);
  assert.match(homeScript, /onShareAppMessage/);

  assert.match(memory, /分享选中日期的回顾/);
  assert.match(memory, /open-type="share"/);
  assert.match(memory, /\{\{item\.preview\}\}/);
  assert.match(memoryScript, /onShareAppMessage/);
  assert.match(memoryScript, /preview: item\.preview \|\| ""/);
});
