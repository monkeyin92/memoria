const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");
const childMemory = require("../utils/child-memory");
const { CONTROL_API_BASE_URL } = require("../config");

const root = path.join(__dirname, "..");
const storage = {};
let pageDefinition = null;

async function withWx(fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const previousPage = global.Page;
  const navigations = [];
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
    navigateTo(options) {
      navigations.push(options);
    },
    navigateBack(options) {
      options?.fail?.();
    },
    showModal(options) {
      options.success?.({ confirm: true });
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner", display_name: "家长" },
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
    return await fn(navigations);
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

test("child-memory page is registered and reachable from guardian home", () => {
  const appConfig = require("../app.json");
  assert.ok(appConfig.pages.includes("pages/child-memory/index"));
  const guardian = fs.readFileSync(path.join(root, "pages/guardian/index.wxml"), "utf8");
  const guardianJs = fs.readFileSync(path.join(root, "pages/guardian/index.js"), "utf8");
  assert.match(guardian, /孩子的记忆/);
  assert.match(guardian, /openChildMemory/);
  assert.match(guardianJs, /pages\/child-memory\/index/);
});

test("normalizeSessionSummaries sorts newest first and rejects transcript marker", () => {
  const items = childMemory.normalizeSessionSummaries({
    privacy: { contains_transcript: false },
    items: [
      {
        record_id: "old",
        text: "孩子聊了画画。",
        session_id: "s1",
        created_at: "2026-10-01T10:00:00+00:00",
      },
      {
        record_id: "new",
        text: "孩子问起恐龙。",
        session_id: "s2",
        created_at: "2026-10-12T10:00:00+00:00",
      },
    ],
  });
  assert.deepEqual(
    items.map((item) => item.recordId),
    ["new", "old"],
  );
  assert.doesNotThrow(() => childMemory.assertsNoTranscriptFields(items));
  assert.throws(
    () =>
      childMemory.normalizeSessionSummaries({
        privacy: { contains_transcript: true },
        items: [],
      }),
    /原文/,
  );
});

test("normalizeChildArchive keeps whitelist labels and last_mentioned_at", () => {
  const items = childMemory.normalizeChildArchive({
    items: [
      {
        record_id: "a1",
        field: "preference",
        text: "喜欢恐龙",
        created_at: "2026-10-01T10:00:00+00:00",
        last_mentioned_at: "2026-10-12T08:00:00+00:00",
      },
      {
        record_id: "bad",
        field: "school",
        text: "某某小学",
        created_at: "2026-10-01T10:00:00+00:00",
      },
    ],
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].fieldLabel, "喜欢什么");
  assert.match(items[0].lastMentionedLabel, /10月12日/);
});

test("child memory API helpers hit the guardian layer endpoints", async () => {
  await withWx(async () => {
    const calls = [];
    global.wx.request = (options) => {
      calls.push({
        url: options.url,
        method: options.method || "GET",
      });
      options.success({
        statusCode: 200,
        data: { items: [], privacy: { contains_transcript: false } },
      });
    };

    await api.getChildSessionSummaries("person/child");
    await api.deleteChildSessionSummary("person/child", "rec/1");
    await api.clearChildSessionSummaries("person/child");
    await api.getChildArchive("person/child");
    await api.deleteChildArchiveFact("person/child", "rec/2");
    await api.clearChildArchive("person/child");
    await api.touchChildActivity("person/child");

    assert.equal(
      calls[0].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/session-summaries?limit=20`,
    );
    assert.equal(calls[1].method, "DELETE");
    assert.match(calls[1].url, /session-summaries\/rec%2F1$/);
    assert.equal(calls[2].method, "DELETE");
    assert.match(calls[2].url, /session-summaries$/);
    assert.match(calls[3].url, /child-archive\?limit=20$/);
    assert.match(calls[4].url, /child-archive\/rec%2F2$/);
    assert.equal(calls[5].method, "DELETE");
    assert.match(calls[5].url, /child-archive$/);
    assert.match(calls[6].url, /activity\?source=miniprogram$/);
    await assert.rejects(api.deleteChildSessionSummary("", "rec"), TypeError);
    await assert.rejects(api.clearChildArchive(""), TypeError);
  });
});

test("page lists summaries, hides archive without consent, and deletes with confirm", async () => {
  await withWx(async () => {
    const deleted = [];
    const restore = stubApi({
      getChildSessionSummaries: async () => ({
        privacy: { contains_transcript: false },
        items: [
          {
            record_id: "sum_1",
            text: "孩子今天聊了小狗和画画。",
            session_id: "sess",
            created_at: "2026-10-12T09:00:00+00:00",
          },
        ],
      }),
      getPersonConsents: async () => ({ items: [] }),
      getChildArchive: async () => {
        throw new Error("archive should not load without consent");
      },
      touchChildActivity: async () => ({}),
      deleteChildSessionSummary: async (minorId, recordId) => {
        deleted.push({ minorId, recordId });
        return { deleted: true };
      },
      clearChildSessionSummaries: async () => ({ deleted: true }),
      grantPersonConsent: async () => ({}),
      revokePersonConsent: async () => ({}),
    });
    try {
      const page = instantiate(loadPage("../pages/child-memory/index"));
      page.onLoad({ minorId: "person_child", name: encodeURIComponent("豆豆") });
      assert.equal(page.data.minorId, "person_child");
      assert.equal(page.data.childName, "豆豆");
      await page.refresh();
      assert.equal(page.data.loadState, "ready");
      assert.equal(page.data.summaries.length, 1);
      assert.equal(page.data.archiveConsent.active, false);
      assert.equal(page.data.archive.length, 0);
      assert.doesNotMatch(JSON.stringify(page.data), /transcript|utterance|audio_url/);

      page.askDeleteSummary({ currentTarget: { dataset: { recordId: "sum_1" } } });
      assert.equal(page.data.confirm.kind, "delete-summary");
      await page.runConfirm();
      assert.deepEqual(deleted, [{ minorId: "person_child", recordId: "sum_1" }]);
      assert.equal(page.data.confirm, null);
    } finally {
      restore();
    }
  });
});

test("consent on loads archive; consent off keeps archive empty and prompts", async () => {
  await withWx(async () => {
    let archiveCalls = 0;
    const restore = stubApi({
      getChildSessionSummaries: async () => ({
        privacy: { contains_transcript: false },
        items: [],
      }),
      getPersonConsents: async () => ({
        items: [
          {
            consent_kind: "child_archive",
            consent_id: "c_archive",
            active: true,
            granted_at: "2026-10-12T00:00:00+00:00",
          },
        ],
      }),
      getChildArchive: async () => {
        archiveCalls += 1;
        return {
          items: [
            {
              record_id: "arc_1",
              field: "preference",
              text: "喜欢恐龙",
              created_at: "2026-10-10T00:00:00+00:00",
              last_mentioned_at: "2026-10-12T08:00:00+00:00",
            },
          ],
        };
      },
      touchChildActivity: async () => ({}),
      clearChildArchive: async () => ({ deleted: true }),
    });
    try {
      const page = instantiate(loadPage("../pages/child-memory/index"));
      page.onLoad({ minorId: "person_child", name: "豆豆" });
      await page.refresh();
      assert.equal(archiveCalls, 1);
      assert.equal(page.data.archiveConsent.active, true);
      assert.equal(page.data.archive[0].text, "喜欢恐龙");
      assert.match(page.data.archive[0].lastMentionedLabel, /10月12日/);

      page.askClearArchive();
      assert.equal(page.data.confirm.kind, "clear-archive");
      await page.runConfirm();
      assert.equal(page.data.confirm, null);
    } finally {
      restore();
    }
  });
});

test("WXML never renders transcript fields and keeps friendly empty copy", () => {
  const wxml = fs.readFileSync(path.join(root, "pages/child-memory/index.wxml"), "utf8");
  assert.doesNotMatch(wxml, /transcript|utterance|audio_url|user_text|assistant_text/);
  assert.match(wxml, /还没有会话小结/);
  assert.match(wxml, /长期小档案已关闭/);
  assert.match(wxml, /小档案还是空的/);
  assert.match(wxml, /看得见 · 删得掉/);
  assert.match(wxml, /服务器不保存孩子的录音和原话/);
});
