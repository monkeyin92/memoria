const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");

const root = path.join(__dirname, "..");
const storage = {};
let pageDefinition = null;

async function withWx(fn) {
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
    return await fn();
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
  }
}

async function withTimezone(timezone, fn) {
  const previousTimezone = process.env.TZ;
  process.env.TZ = timezone;
  try {
    return await fn();
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = previousTimezone;
  }
}

function createDeferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
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

function readyBindingState() {
  return {
    status: "ready",
    binding: {
      binding_id: "bd_1",
      device_id: "dev_1",
      declared_mode: "self_use",
    },
  };
}

function homeApiStubs(overrides = {}) {
  return {
    currentIdentity: () => ({ user_id: "person_owner", display_name: "主人" }),
    currentAuthEpoch: () => 0,
    isAuthEpochCurrent: () => true,
    syncDeviceBindings: async () => readyBindingState(),
    getActivationStatus: async () => ({ status: "ready_for_conversation" }),
    getRuntimeProfile: async () => ({}),
    getDeviceSettings: async () => ({}),
    getProfile: async () => ({}),
    getDeviceDiagnostics: async () => ({}),
    requireRuntimeCapability: async () => allowedGate(),
    getMemoryDays: async () => ({ items: [] }),
    getConversationReview: async () => ({ memory_candidates: [] }),
    getConversationSessions: async () => ({ items: [] }),
    ...overrides,
  };
}

function todayKey(date = new Date()) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

test("home today count uses session reads and composes the daily summary", async () => {
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

test("loadHome wires a non-UTC local-day session through page data into sharing", async () => {
  await withTimezone("Asia/Shanghai", async () => {
    await withWx(async () => {
      const definition = loadPage("../pages/home/index");
      const page = instantiate(definition);
      const localNow = new Date();
      const localTodayAt0030 = new Date(
        localNow.getFullYear(),
        localNow.getMonth(),
        localNow.getDate(),
        0,
        30,
      );
      const localYesterdayAt2330 = new Date(
        localNow.getFullYear(),
        localNow.getMonth(),
        localNow.getDate() - 1,
        23,
        30,
      );
      assert.notEqual(
        localTodayAt0030.toISOString().slice(0, 10),
        todayKey(localTodayAt0030),
        "夹具必须跨 UTC 日界线，才能证明按客户端本地日统计",
      );
      const overview = "他提到数学考了 95 分。";
      const restore = stubApi(
        homeApiStubs({
          getMemoryDays: async () => ({
            items: [
              {
                day: todayKey(),
                message_count: 9,
                summary: { title: "今天聊了考试", overview },
              },
            ],
          }),
          getConversationSessions: async () => ({
            items: [
              { session_id: "s-today", occurred_at: localTodayAt0030.toISOString() },
              { session_id: "s-yesterday", occurred_at: localYesterdayAt2330.toISOString() },
            ],
          }),
        }),
      );
      try {
        page.setData({ authenticated: true });
        await page.loadHome();

        assert.equal(page.data.todayCount, 1, "UTC 前一日的时间戳属于客户端本地今天");
        assert.equal(page.data.todayMeta, "今天 · 1 次对话");
        assert.equal(
          page.data.dailySummaryText,
          `今天和 Memoria 聊了 1 次。${overview}`,
        );
        const shared = page.onShareAppMessage();
        assert.equal(shared.path, "/pages/home/index");
        assert.equal(shared.title, `今天和 Memoria 聊了 1 次。${overview}`);
      } finally {
        restore();
      }
    });
  });
});

test("loadHome falls back to the day count when the session list is unavailable", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const restore = stubApi(
      homeApiStubs({
        getMemoryDays: async () => ({ items: [{ day: todayKey(), message_count: 3 }] }),
        getConversationSessions: async () => {
          throw new Error("session index unavailable");
        },
      }),
    );
    try {
      page.setData({ authenticated: true });
      await page.loadHome();
      assert.equal(page.data.todayCount, 3);
      assert.equal(page.data.todayMeta, "今天 · 3 次对话");
      assert.equal(
        page.data.dailySummaryText,
        "今天和 Memoria 聊了 3 次。回顾还没生成，可在回顾页生成。",
        "没有服务端回顾时不得编造摘要内容",
      );
    } finally {
      restore();
    }
  });
});

test("guest and failed home loads cannot retain a private summary", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const privateState = {
      authenticated: true,
      todayCount: 2,
      todayTitle: "考试回顾",
      todayOverview: "数学考了 95 分。",
      dailySummaryText: "今天和 Memoria 聊了 2 次。数学考了 95 分。",
    };

    const guestPage = instantiate(definition);
    guestPage.setData(privateState);
    let restore = stubApi({
      hasAuthenticatedSession: () => false,
      currentIdentity: () => null,
    });
    try {
      guestPage.onShow();
      assert.equal(guestPage.data.authenticated, false);
      assert.equal(guestPage.data.todayCount, 0);
      assert.equal(guestPage.data.dailySummaryText, "");
      assert.doesNotMatch(guestPage.onShareAppMessage().title, /数学|95 分|聊了/);
    } finally {
      restore();
    }

    const failedPage = instantiate(definition);
    failedPage.setData(privateState);
    restore = stubApi(
      homeApiStubs({
        getMemoryDays: async () => {
          throw new Error("memory day unavailable");
        },
      }),
    );
    try {
      await failedPage.loadHome();
      assert.equal(failedPage.data.todayCount, 0);
      assert.equal(failedPage.data.todayTitle, "");
      assert.equal(failedPage.data.todayOverview, "");
      assert.equal(failedPage.data.dailySummaryText, "");
      assert.doesNotMatch(failedPage.onShareAppMessage().title, /数学|95 分|聊了/);
    } finally {
      restore();
    }
  });
});

test("a late home response cannot repopulate a private summary after auth is cleared", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const days = createDeferred();
    const daysStarted = createDeferred();
    const auth = { authenticated: true, epoch: 7 };
    const restore = stubApi(
      homeApiStubs({
        currentAuthEpoch: () => auth.epoch,
        isAuthEpochCurrent: (epoch) => auth.authenticated && epoch === auth.epoch,
        getMemoryDays: async () => {
          daysStarted.resolve();
          return days.promise;
        },
        getConversationSessions: async () => ({
          items: [{ session_id: "s-late", occurred_at: new Date().toISOString() }],
        }),
      }),
    );
    try {
      page.setData({ authenticated: true });
      const loading = page.loadHome();
      await daysStarted.promise;

      auth.authenticated = false;
      auth.epoch += 1;
      page._enterGuestState();
      days.resolve({
        items: [
          {
            day: todayKey(),
            message_count: 1,
            summary: { title: "迟到回顾", overview: "不应回到游客首页。" },
          },
        ],
      });
      await loading;

      assert.equal(page.data.authenticated, false);
      assert.equal(page.data.todayCount, 0);
      assert.equal(page.data.dailySummaryText, "");
      assert.doesNotMatch(page.onShareAppMessage().title, /迟到|游客首页|聊了/);
    } finally {
      restore();
    }
  });
});

test("auth clear while resolving a custom persona cannot repopulate private home data", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const personas = createDeferred();
    const personasStarted = createDeferred();
    const auth = { authenticated: true, epoch: 7 };
    const restore = stubApi(
      homeApiStubs({
        currentAuthEpoch: () => auth.epoch,
        isAuthEpochCurrent: (epoch) => auth.authenticated && epoch === auth.epoch,
        getRuntimeProfile: async () => ({ persona: { persona_id: "cu_private" } }),
        getMemoryDays: async () => ({
          items: [
            {
              day: todayKey(),
              message_count: 1,
              summary: { overview: "不应回到游客首页。" },
            },
          ],
        }),
        getConversationSessions: async () => ({
          items: [{ session_id: "s-private", occurred_at: new Date().toISOString() }],
        }),
        listPersonas: async () => {
          personasStarted.resolve();
          return personas.promise;
        },
      }),
    );
    try {
      page.setData({ authenticated: true });
      const loading = page.loadHome();
      await personasStarted.promise;

      auth.authenticated = false;
      auth.epoch += 1;
      page._enterGuestState();
      personas.resolve({
        custom_personas: [{ persona_id: "cu_private", display_name: "私人角色" }],
      });
      await loading;

      assert.equal(page.data.authenticated, false);
      assert.equal(page.data.todayCount, 0);
      assert.equal(page.data.dailySummaryText, "");
    } finally {
      restore();
    }
  });
});

test("an older home flow cannot overwrite the latest private summary", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    const firstSync = createDeferred();
    const firstSyncStarted = createDeferred();
    let syncCalls = 0;
    let daysCalls = 0;
    const restore = stubApi(
      homeApiStubs({
        syncDeviceBindings: async () => {
          syncCalls += 1;
          if (syncCalls === 1) {
            firstSyncStarted.resolve();
            return firstSync.promise;
          }
          return readyBindingState();
        },
        getMemoryDays: async () => {
          daysCalls += 1;
          return {
            items: [
              {
                day: todayKey(),
                message_count: 1,
                summary: {
                  title: "最新回顾",
                  overview: "第二次加载的摘要。",
                },
              },
            ],
          };
        },
        getConversationSessions: async () => ({ items: [] }),
      }),
    );
    try {
      page.setData({ authenticated: true });
      const older = page.loadHome();
      await firstSyncStarted.promise;
      const latest = page.loadHome();
      await latest;
      assert.equal(page.data.dailySummaryText, "今天和 Memoria 聊了 1 次。第二次加载的摘要。");

      firstSync.resolve(readyBindingState());
      await older;
      assert.equal(daysCalls, 1, "旧 flow 应在绑定响应后被 flowSeq 丢弃");
      assert.equal(page.data.todayCount, 1);
      assert.equal(page.data.dailySummaryText, "今天和 Memoria 聊了 1 次。第二次加载的摘要。");
    } finally {
      restore();
    }
  });
});

test("home reads the daily summary without consulting the Runtime Profile", async () => {
  await withWx(async () => {
    const definition = loadPage("../pages/home/index");
    const page = instantiate(definition);
    let memoryCalls = 0;
    let gateCalls = 0;
    const restore = stubApi({
      requireRuntimeCapability: async () => {
        gateCalls += 1;
        return { allowed: false, reason: "capability_missing" };
      },
      getMemoryDays: async () => {
        memoryCalls += 1;
        return { items: [] };
      },
      getConversationReview: async () => ({ memory_candidates: [] }),
      getConversationSessions: async () => ({ items: [] }),
    });
    try {
      await page._loadToday({ user_id: "person_owner" });
      assert.equal(gateCalls, 0);
      assert.equal(memoryCalls, 1);
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
