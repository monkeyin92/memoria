const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");
const { CONTROL_API_BASE_URL } = require("../config");
const { guardianSubjectFromBinding } = require("../utils/guardian");
const { formatBeijingTime } = require("../utils/memory-lifecycle");

const ROOT = path.join(__dirname, "..");
const LIFECYCLE_DIR = path.join(ROOT, "pages/memory-lifecycle");
const LIFECYCLE_WXML = path.join(LIFECYCLE_DIR, "index.wxml");
const LIFECYCLE_JS = path.join(LIFECYCLE_DIR, "index.js");
const LIFECYCLE_FILES = ["index.js", "index.json", "index.wxml", "index.wxss"];

const BINDING = {
  declared_mode: "parent_for_child",
  status: "active",
  account_owner_id: "person_owner",
  primary_subject_ids: ["person_owner", "person_child"],
};

const OTHER_BINDING = {
  ...BINDING,
  primary_subject_ids: ["person_owner", "person_other"],
};

const MEMORIES = {
  candidates: [
    {
      claim_id: "claim_1",
      value: "喜欢蓝色",
      status: "candidate",
      reason: "pending_confirmation",
      domain_category: "preference",
    },
  ],
  confirmed: [
    {
      memory_id: "mem_1",
      title: "生日",
      snippet: "10月19日一起过了生日",
      status: "confirmed",
      domain_category: "event",
      occurred_at: "2026-10-19T13:05:00Z",
    },
  ],
};

const RETRACTED = {
  subject_id: "person_child",
  items: [
    {
      claim_id: "claim_1",
      title: "喜欢蓝色",
      value: "喜欢蓝色",
      domain_category: "preference",
      retracted_at: "2026-10-19T13:05:00Z",
    },
  ],
  privacy: { contains_transcript: false },
};

const storage = {};
let pageDefinition = null;

async function withWx(fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  for (const key of Object.keys(storage)) delete storage[key];
  const app = {
    globalData: {
      identity: { user_id: "person_owner", display_name: "妈妈" },
      accessToken: "token",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
      openUnbindSheetOnDevice: false,
    },
    subscribeAuthCleared: () => () => {},
  };
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
    removeStorageSync: (key) => {
      delete storage[key];
    },
    showToast() {},
    navigateTo() {},
    switchTab() {},
    showModal(options) {
      options.success?.({ confirm: true });
    },
    request() {},
  };
  global.getApp = () => app;
  global.Page = (definition) => {
    pageDefinition = definition;
  };
  try {
    return await fn(app);
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
    for (const [key, value] of Object.entries(updates)) {
      const parts = key.replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
      let cursor = instance.data;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        if (typeof cursor[part] !== "object" || cursor[part] === null) {
          cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
        }
        cursor = cursor[part];
      }
      cursor[parts[parts.length - 1]] = value;
    }
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

function lifecycleSource() {
  return LIFECYCLE_FILES.map((file) => fs.readFileSync(path.join(LIFECYCLE_DIR, file), "utf8")).join(
    "\n",
  );
}

test("the interaction draft and its detail page are gone", () => {
  for (const file of fs.readdirSync(LIFECYCLE_DIR)) {
    assert.doesNotMatch(file, /operation-detail/);
  }
  assert.equal(fs.existsSync(path.join(LIFECYCLE_DIR, "operation-detail.js")), false);
  const appConfig = JSON.parse(fs.readFileSync(path.join(ROOT, "app.json"), "utf8"));
  assert.ok(appConfig.pages.includes("pages/memory-lifecycle/index"));
  assert.equal(appConfig.pages.includes("pages/memory-lifecycle/operation-detail"), false);

  const source = lifecycleSource();
  assert.doesNotMatch(
    source,
    /内部交互草稿|虚构测试数据|【模拟状态】|isDraft|draftLabel|draft-banner|模拟数据/,
  );
});

test("the lifecycle page never uses destructive copy", () => {
  const source = lifecycleSource();
  assert.doesNotMatch(source, /已删除|彻底清除|永久删除/);
  const wxml = fs.readFileSync(LIFECYCLE_WXML, "utf8");
  assert.doesNotMatch(wxml, /已删除|彻底清除|永久删除/);
});

test("both entry points open the lifecycle page", async () => {
  const memoryWxml = fs.readFileSync(path.join(ROOT, "pages/memory/index.wxml"), "utf8");
  const memoryJs = fs.readFileSync(path.join(ROOT, "pages/memory/index.js"), "utf8");
  assert.match(memoryWxml, /wx:if="\{\{guardianMode\}\}"[\s\S]*?bindtap="openMemoryLifecycle"/);
  assert.match(memoryWxml, /<text class="row-title">记忆管理<\/text>/);
  assert.match(memoryWxml, /查看、确认或撤回机器人记住的事/);
  assert.match(memoryJs, /openMemoryLifecycle\(\)[\s\S]*?\/pages\/memory-lifecycle\/index/);

  const profileWxml = fs.readFileSync(path.join(ROOT, "pages/profile/index.wxml"), "utf8");
  const profileJs = fs.readFileSync(path.join(ROOT, "pages/profile/index.js"), "utf8");
  assert.match(
    profileWxml,
    /wx:if="\{\{memoryLifecycleEntryAllowed\}\}"[\s\S]*?bindtap="openMemoryLifecycle"/,
  );
  assert.match(profileWxml, /<text class="row-title">记忆管理<\/text>/);
  assert.match(profileWxml, /查看、确认或撤回机器人记住的事/);
  assert.match(profileJs, /memoryLifecycleEntryAllowed:/);
  assert.match(profileJs, /openMemoryLifecycle\(\)[\s\S]*?\/pages\/memory-lifecycle\/index/);

  await withWx(async () => {
    const opened = [];
    global.wx.navigateTo = (options) => opened.push(options.url);

    const memoryPage = instantiate(loadPage("../pages/memory/index"));
    memoryPage.openMemoryLifecycle();

    const profilePage = instantiate(loadPage("../pages/profile/index"));
    profilePage.setData({ guardianEntryAllowed: true, memoryLifecycleEntryAllowed: true });
    const restore = stubApi({ hasAuthenticatedSession: () => true });
    try {
      await profilePage.openMemoryLifecycle();
    } finally {
      restore();
    }

    assert.deepEqual(opened, [
      "/pages/memory-lifecycle/index",
      "/pages/memory-lifecycle/index",
    ]);
  });
});

test("only an active parent_for_child binding yields a non-owner child", () => {
  assert.equal(guardianSubjectFromBinding(BINDING), "person_child");
  assert.equal(guardianSubjectFromBinding({ ...BINDING, declared_mode: "self_use" }), "");
  assert.equal(guardianSubjectFromBinding({ ...BINDING, status: "revoked" }), "");
  assert.equal(guardianSubjectFromBinding({ ...BINDING, primary_subject_ids: ["person_owner"] }), "");
  assert.equal(guardianSubjectFromBinding(null), "");
});

test("an empty list renders the empty state without throwing", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const calls = [];
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async (minorUserId, limit) => {
        calls.push([minorUserId, limit]);
        return { candidates: [], confirmed: [] };
      },
      getGuardianChildRetractedMemories: async () => ({ items: [] }),
    });
    try {
      assert.equal(await page.loadMemories(), "ok");
      assert.deepEqual(calls, [["person_child", 50]]);
      assert.equal(page.data.isEmpty, true);
      assert.equal(page.data.error, "");
      assert.deepEqual(page.data.pendingItems, []);
      assert.deepEqual(page.data.confirmedItems, []);
      assert.match(fs.readFileSync(LIFECYCLE_WXML, "utf8"), /还没有需要管理的记忆/);
    } finally {
      restore();
    }
  });
});

test("without a bound child the page says how to get one and makes no request", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    let requested = false;
    const restore = stubApi({
      readBindingManifest: () => null,
      hasAuthenticatedSession: () => true,
      getGuardianChildMemories: async () => {
        requested = true;
        return { candidates: [], confirmed: [] };
      },
      getGuardianChildRetractedMemories: async () => {
        requested = true;
        return { items: [] };
      },
    });
    try {
      assert.equal(await page.loadMemories(), "unavailable");
      assert.equal(requested, false);
      assert.equal(page.data.guardianMode, false);
      assert.equal(page.data.unavailable, "绑定设备后可以在这里管理孩子的记忆");
      assert.match(
        fs.readFileSync(LIFECYCLE_JS, "utf8"),
        /绑定设备后可以在这里管理孩子的记忆/,
      );
    } finally {
      restore();
    }
  });
});

test("a 403 or a failed list clears the rows and offers a retry", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const forbidden = new Error("guardian_memory_review_unavailable");
    forbidden.status = 403;
    forbidden.code = "guardian_memory_review_unavailable";
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => {
        throw forbidden;
      },
      getGuardianChildRetractedMemories: async () => ({ items: [] }),
    });
    try {
      page.setData({ pendingItems: [{ id: "stale" }], confirmedItems: [{ id: "stale" }] });
      assert.equal(await page.loadMemories(), "error");
      assert.match(page.data.error, /暂时不能在这里管理/);
      assert.deepEqual(page.data.pendingItems, []);
      assert.deepEqual(page.data.confirmedItems, []);
      assert.equal(page.data.isEmpty, false);
      assert.match(fs.readFileSync(LIFECYCLE_WXML, "utf8"), /bindtap="loadMemories">重试<\/button>/);
    } finally {
      restore();
    }
  });
});

test("a retraction refreshes the authoritative list and is restorable", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const reviewCalls = [];
    const snapshots = [];
    let listCalls = 0;
    let retractedCalls = 0;
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => {
        listCalls += 1;
        snapshots.push({
          pending: page.data.pendingItems.map((item) => item.statusLabel),
          retracted: page.data.retractedItems.map((item) => item.statusLabel),
        });
        return MEMORIES;
      },
      getGuardianChildRetractedMemories: async () => {
        retractedCalls += 1;
        return retractedCalls === 1 ? { items: [] } : RETRACTED;
      },
      reviewGuardianChildMemory: async (minorUserId, claimId, action) => {
        reviewCalls.push([minorUserId, claimId, action]);
        return {
          claim_id: claimId,
          status: "retracted",
          reviewed_at: "2026-10-19T13:05:00Z",
        };
      },
    });
    try {
      assert.equal(await page.loadMemories(), "ok");
      assert.equal(page.data.pendingItems[0].statusLabel, "待确认");
      assert.equal(page.data.confirmedItems[0].occurredAtLabel, "10月19日 21:05");

      const modalPrompts = [];
      global.wx.showModal = (options) => {
        modalPrompts.push(options.content);
        options.success?.({ confirm: true });
      };
      await page.retractMemory({
        currentTarget: { dataset: { id: "claim_1", kind: "candidate" } },
      });

      assert.deepEqual(modalPrompts, ["撤回后机器人不会用到这条。"]);
      assert.deepEqual(reviewCalls, [["person_child", "claim_1", "retract"]]);
      assert.equal(listCalls, 2, "the authoritative list is read again after a retraction");
      assert.equal(
        snapshots[1].pending[0],
        "待确认",
        "nothing is edited locally before the server read",
      );
      assert.equal(page.data.pendingItems.length, 0);
      assert.equal(page.data.confirmedItems.length, 1);
      assert.equal(page.data.retractedItems.length, 1);
      assert.equal(page.data.retractedItems[0].statusLabel, "已撤回");
      assert.equal(page.data.retractedItems[0].retractedAtLabel, "10月19日 21:05");
      assert.equal(page.data.reviewingId, "");

      const wxml = fs.readFileSync(LIFECYCLE_WXML, "utf8");
      assert.match(wxml, /bindtap="restoreMemory"/);
      assert.match(wxml, /撤回的记忆机器人不会用到，可以随时恢复。/);
      assert.doesNotMatch(wxml, /暂时不会提起/);
      assert.doesNotMatch(wxml, /恢复功能准备中/);
    } finally {
      restore();
    }
  });
});

test("a reopened page reads retracted memories from the backend", async () => {
  await withWx(async () => {
    const calls = [];
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => ({ candidates: [], confirmed: [] }),
      getGuardianChildRetractedMemories: async (minorUserId, limit) => {
        calls.push([minorUserId, limit]);
        return RETRACTED;
      },
    });
    try {
      const first = instantiate(loadPage("../pages/memory-lifecycle/index"));
      first.onLoad();
      assert.equal(await first.loadMemories(), "ok");
      assert.equal(first.data.retractedItems.length, 1);

      const reopened = instantiate(loadPage("../pages/memory-lifecycle/index"));
      reopened.onLoad();
      reopened.onShow();
      await new Promise((resolve) => setImmediate(resolve));

      assert.deepEqual(calls, [
        ["person_child", 20],
        ["person_child", 20],
      ]);
      assert.equal(reopened.data.retractedItems.length, 1);
      assert.equal(reopened.data.retractedItems[0].id, "claim_1");
      assert.equal(reopened.data.retractedItems[0].title, "喜欢蓝色");
      assert.equal(reopened.data.retractedItems[0].retractedAtLabel, "10月19日 21:05");
      assert.equal(reopened.data.isEmpty, false);
      assert.doesNotMatch(JSON.stringify(reopened.data), /NaN/);
    } finally {
      restore();
    }
  });
});

test("restoring a retracted memory confirms it and reloads both lists", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const reviewCalls = [];
    const toasts = [];
    let listCalls = 0;
    let retractedCalls = 0;
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => {
        listCalls += 1;
        return { candidates: [], confirmed: [] };
      },
      getGuardianChildRetractedMemories: async () => {
        retractedCalls += 1;
        return retractedCalls === 1 ? RETRACTED : { items: [] };
      },
      reviewGuardianChildMemory: async (minorUserId, claimId, action) => {
        reviewCalls.push([minorUserId, claimId, action]);
        return {
          claim_id: claimId,
          status: "confirmed",
          reviewed_at: "2026-10-19T13:05:00Z",
        };
      },
    });
    try {
      assert.equal(await page.loadMemories(), "ok");
      assert.equal(page.data.retractedItems.length, 1);

      const modalOptions = [];
      global.wx.showModal = (options) => {
        modalOptions.push(options);
        options.success?.({ confirm: true });
      };
      global.wx.showToast = (options) => toasts.push(options.title);

      await page.restoreMemory({ currentTarget: { dataset: { id: "claim_1" } } });

      assert.equal(modalOptions.length, 1);
      assert.equal(modalOptions[0].content, "恢复后机器人又可以用到这条。");
      assert.equal(modalOptions[0].confirmText, "恢复");
      assert.deepEqual(reviewCalls, [["person_child", "claim_1", "confirm"]]);
      assert.equal(listCalls, 2, "the list is read again after a restore");
      assert.equal(retractedCalls, 2, "the retracted list is read again after a restore");
      assert.deepEqual(page.data.retractedItems, []);
      assert.equal(page.data.isEmpty, true);
      assert.equal(page.data.reviewingId, "");
      assert.deepEqual(toasts, ["已恢复"]);
      assert.doesNotMatch(fs.readFileSync(LIFECYCLE_WXML, "utf8"), />删除</);
    } finally {
      restore();
    }
  });
});

test("a failed retracted read surfaces the error without NaN or a crash", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    let fail = true;
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => ({ candidates: [], confirmed: [] }),
      getGuardianChildRetractedMemories: async () => {
        if (fail) throw new Error("retracted unavailable");
        return {
          items: [
            { claim_id: "claim_bad", title: "", value: "", retracted_at: "not-a-date" },
          ],
        };
      },
    });
    try {
      assert.equal(await page.loadMemories(), "error");
      assert.match(page.data.error, /retracted unavailable/);
      assert.deepEqual(page.data.retractedItems, []);
      assert.doesNotMatch(JSON.stringify(page.data), /NaN/);

      fail = false;
      assert.equal(await page.loadMemories(), "ok");
      assert.equal(page.data.retractedItems.length, 1);
      assert.equal(page.data.retractedItems[0].retractedAtLabel, "");
      assert.doesNotMatch(JSON.stringify(page.data), /NaN/);
    } finally {
      restore();
    }
  });
});

test("ISO times become Beijing labels and bad input never prints NaN", () => {
  assert.equal(formatBeijingTime("2026-10-19T13:05:00Z"), "10月19日 21:05");
  assert.equal(formatBeijingTime("2026-10-19T21:05:00+08:00"), "10月19日 21:05");
  assert.equal(formatBeijingTime("2026-10-19T16:30:00Z"), "10月20日 00:30");
  assert.equal(formatBeijingTime("2026-10-19T13:05:00.123Z"), "10月19日 21:05");
  assert.equal(formatBeijingTime("2026-10-19T13:05:07.123456Z"), "10月19日 21:05");
  assert.equal(formatBeijingTime("2026-10-19T13:05:07.123456+00:00"), "10月19日 21:05");
  assert.equal(
    formatBeijingTime("2026-10-19T13:05:59.999999Z"),
    "10月19日 21:05",
    "microseconds are truncated to milliseconds, not rounded",
  );
  for (const value of [
    "",
    null,
    undefined,
    "2026-10-19 08:00:00",
    "2026-10-19",
    "not-a-date",
    "2026-02-31T00:00:00Z",
    "2026-10-19T13:05:07.1234567Z",
    "2026-10-19T13:05:07.1234567+00:00",
  ]) {
    const label = formatBeijingTime(value);
    assert.equal(label, "");
    assert.doesNotMatch(label, /NaN/);
  }
});

test("the lifecycle WXML carries no WXS new Date", () => {
  const wxml = fs.readFileSync(LIFECYCLE_WXML, "utf8");
  assert.doesNotMatch(wxml, /new Date/);
  const blocks = [...wxml.matchAll(/<wxs\b[^>]*>([\s\S]*?)<\/wxs>/gi)];
  for (const block of blocks) {
    assert.doesNotMatch(block[1], /new Date/);
  }
});

test("the guardian request URL carries the currently bound minor id, and another child is unavailable", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const requests = [];
    global.wx.request = (options) => {
      requests.push(options);
      if (options.url.includes("/person_child/") && options.url.includes("/retracted?")) {
        options.success({ statusCode: 200, data: RETRACTED });
      } else if (options.url.includes("/person_child/memories?")) {
        options.success({ statusCode: 200, data: MEMORIES });
      } else {
        options.success({
          statusCode: 403,
          data: { detail: { code: "guardian_memory_review_unavailable" } },
        });
      }
    };
    let binding = BINDING;
    const restore = stubApi({
      readBindingManifest: () => binding,
    });
    try {
      assert.equal(await page.loadMemories(), "ok");
      assert.equal(
        requests[0].url,
        `${CONTROL_API_BASE_URL}/v1/guardian/minors/person_child/memories?limit=50`,
      );
      assert.equal(requests[0].method, "GET");
      assert.equal(
        requests[1].url,
        `${CONTROL_API_BASE_URL}/v1/guardian/minors/person_child/memories/retracted?limit=20`,
      );
      assert.equal(requests[1].method, "GET");
      assert.equal(page.data.confirmedItems.length, 1);
      assert.equal(page.data.retractedItems.length, 1);

      binding = OTHER_BINDING;
      assert.equal(await page.loadMemories(), "error");
      assert.equal(
        requests[2].url,
        `${CONTROL_API_BASE_URL}/v1/guardian/minors/person_other/memories?limit=50`,
      );
      assert.equal(
        requests[3].url,
        `${CONTROL_API_BASE_URL}/v1/guardian/minors/person_other/memories/retracted?limit=20`,
      );
      assert.match(page.data.error, /暂时不能在这里管理/);
      assert.deepEqual(page.data.pendingItems, []);
      assert.deepEqual(page.data.confirmedItems, []);
      assert.deepEqual(page.data.retractedItems, []);
    } finally {
      restore();
    }
  });
});

test("the bottom connection to the unbind deletion path opens the device tab's unbind sheet", async () => {
  await withWx(async (app) => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const tabs = [];
    global.wx.switchTab = (options) => tabs.push(options.url);

    page.openUnbindDeletePath();

    assert.deepEqual(tabs, ["/pages/device/index"]);
    assert.equal(app.globalData.openUnbindSheetOnDevice, true);
    const wxml = fs.readFileSync(LIFECYCLE_WXML, "utf8");
    assert.match(wxml, /想把孩子的数据全部删掉，可以在设备页解绑时选择删除数据/);
    assert.match(wxml, /bindtap="openUnbindDeletePath"/);
  });
});

test("the retraction prompt is the same for a candidate and a confirmed memory", async () => {
  await withWx(async () => {
    const page = instantiate(loadPage("../pages/memory-lifecycle/index"));
    const restore = stubApi({
      readBindingManifest: () => BINDING,
      hasAuthenticatedSession: () => true,
      currentAuthEpoch: () => 0,
      isAuthEpochCurrent: () => true,
      getGuardianChildMemories: async () => MEMORIES,
      getGuardianChildRetractedMemories: async () => ({ items: [] }),
    });
    try {
      assert.equal(await page.loadMemories(), "ok");
      const prompts = [];
      global.wx.showModal = (options) => {
        prompts.push(options.content);
        options.success?.({ confirm: false });
      };

      await page.retractMemory({
        currentTarget: { dataset: { id: "claim_1", kind: "candidate" } },
      });
      await page.retractMemory({
        currentTarget: { dataset: { id: "mem_1", kind: "confirmed" } },
      });

      assert.deepEqual(prompts, [
        "撤回后机器人不会用到这条。",
        "撤回后机器人不会用到这条。",
      ]);
    } finally {
      restore();
    }
  });
});
