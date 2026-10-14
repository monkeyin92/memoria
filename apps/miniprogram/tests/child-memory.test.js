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
    navigateBack(options) {
      options?.fail?.();
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

test("home routes unread safety banner and shows the child memory card", () => {
  const home = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const homeJs = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  const appConfig = require("../app.json");
  assert.ok(appConfig.pages.includes("pages/child-memory/index"));
  assert.match(home, /openUnreadSafety|unreadSafetyAlert/);
  assert.match(home, /home-row-card[^>]*bindtap="openChildMemory"/);
  assert.match(home, /的记忆/);
  assert.match(homeJs, /openChildMemory/);
  assert.match(homeJs, /openUnreadSafety/);
  assert.match(homeJs, /boundChildFromManifest/);
});

test("page copy and structure match product acceptance", () => {
  const wxml = fs.readFileSync(path.join(root, "pages/child-memory/index.wxml"), "utf8");
  const wxss = fs.readFileSync(path.join(root, "pages/child-memory/index.wxss"), "utf8");
  assert.match(wxml, /近期摘要/);
  assert.match(wxml, /长期记忆/);
  assert.match(wxml, /privacyLine|cm-privacy/);
  assert.match(wxml, /askClearSummaries/);
  assert.match(wxml, /askDeleteSummary/);
  assert.match(wxml, /mp-sheet/);
  assert.match(wxml, /confirm\.body/);
  assert.doesNotMatch(wxml, /wx:else[^>]*confirm\.body|confirm\.body[^>]*wx:else/);
  assert.match(wxml, /再想想|deleteKeepLabel/);
  assert.doesNotMatch(wxml, /onSummaryTouchStart/);
  assert.doesNotMatch(wxml, /transcript|utterance|audio_url|user_text|assistant_text/);
  assert.doesNotMatch(wxml, /cm-illus/);
  assert.match(wxss, /--mp-bg|var\(--mp-bg\)/);
  assert.doesNotMatch(wxss, /cartoon|completeness|score|radar/i);
  assert.equal(
    childMemory.PRIVACY_LINE,
    "孩子说的原话，我们的服务器在入库前就去掉了；机器人本身不存录音。",
  );
  assert.equal(
    childMemory.BACKUP_RETENTION_LINE,
    "删除的数据会在 7 天内从备份中清除",
  );
  assert.match(wxml, /backupRetentionLine/);
  assert.match(wxml, /cm-muted-link/);
  assert.doesNotMatch(wxml, /cm-danger-link/);
  assert.equal(childMemory.DELETE_KEEP, "再想想");
  assert.equal(childMemory.memoryHeaderTitle("星澜", "小明"), "小明的记忆");
  assert.equal(childMemory.memoryEmptyTitle("星澜", "小明"), "星澜还没有记住小明的事");
  assert.equal(childMemory.memoryHeaderCaption(3), "共 3 条 · 可以随时删除");
  assert.equal(
    childMemory.EMPTY_RECENT,
    "还没有记忆。孩子和 memoria 聊过之后，这里会出现摘要。",
  );
  assert.equal(
    childMemory.DELETE_CONFIRM,
    "删除后，这条记忆会从服务器上删除，无法恢复，不影响其他记忆。",
  );
  assert.equal(childMemory.CLEARED_TOAST, "已清空，剩余 0 条");
  assert.equal(
    childMemory.CONSENT_OFF_DIALOG,
    "关闭后不会再写入新内容。已有内容可以继续查看和删除。",
  );
  assert.equal(childMemory.EMPTY_ARCHIVE_ON, "开启后，孩子聊到的兴趣会出现在这里。");
});

test("normalizeSessionSummaries uses spaced Chinese dates and newest first", () => {
  const items = childMemory.normalizeSessionSummaries({
    privacy: { contains_transcript: false },
    items: [
      {
        record_id: "old",
        text: "孩子聊了画画。",
        created_at: "2026-10-01T10:00:00+00:00",
      },
      {
        record_id: "new",
        text: "孩子问起恐龙。",
        created_at: "2026-10-13T10:00:00+00:00",
      },
    ],
  });
  assert.deepEqual(
    items.map((item) => item.recordId),
    ["new", "old"],
  );
  assert.match(items[0].dateLabel, /10 月 13 日/);
});

test("archive facts group under whitelist categories", () => {
  const flat = childMemory.normalizeChildArchive({
    items: [
      {
        record_id: "a1",
        field: "preference",
        text: "恐龙",
        created_at: "2026-10-01T10:00:00+00:00",
        last_mentioned_at: "2026-10-12T08:00:00+00:00",
      },
      {
        record_id: "a2",
        field: "preference",
        text: "霸王龙",
        created_at: "2026-10-11T10:00:00+00:00",
        last_mentioned_at: "2026-10-13T08:00:00+00:00",
      },
      {
        record_id: "bad",
        field: "school",
        text: "某某小学",
        created_at: "2026-10-01T10:00:00+00:00",
      },
    ],
  });
  const groups = childMemory.groupArchiveByCategory(flat);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].label, "兴趣");
  assert.deepEqual(
    groups[0].items.map((item) => item.text),
    ["霸王龙", "恐龙"],
  );
});

test("child memory API helpers hit the guardian layer endpoints", async () => {
  await withWx(async () => {
    const calls = [];
    global.wx.request = (options) => {
      calls.push({ url: options.url, method: options.method || "GET" });
      options.success({
        statusCode: 200,
        data: { items: [], privacy: { contains_transcript: false } },
      });
    };
    await api.getChildSessionSummaries("person/child");
    await api.deleteChildSessionSummary("person/child", "rec/1");
    await api.clearChildSessionSummaries("person/child");
    await api.getChildArchive("person/child");
    await api.clearChildArchive("person/child");
    assert.equal(
      calls[0].url,
      `${CONTROL_API_BASE_URL}/v1/guardian/minors/person%2Fchild/session-summaries?limit=20`,
    );
    assert.equal(calls[2].method, "DELETE");
    assert.match(calls[4].url, /child-archive$/);
  });
});

test("recent tab lists summaries, delete sheet confirms, clear-all shows remaining 0", async () => {
  await withWx(async () => {
    let summaries = [
      {
        record_id: "sum_1",
        text: "孩子今天聊了小狗和画画。",
        session_id: "sess",
        created_at: "2026-10-13T09:00:00+00:00",
      },
    ];
    const restore = stubApi({
      getChildSessionSummaries: async () => ({
        privacy: { contains_transcript: false },
        items: summaries,
      }),
      getPersonConsents: async () => ({ items: [] }),
      getChildArchive: async () => {
        throw new Error("archive should not load without consent");
      },
      touchChildActivity: async () => ({}),
      deleteChildSessionSummary: async (_minorId, recordId) => {
        summaries = summaries.filter((item) => item.record_id !== recordId);
        return { deleted: true };
      },
      clearChildSessionSummaries: async () => {
        summaries = [];
        return { deleted: true, remaining: { memory_records: 0 } };
      },
    });
    try {
      const page = instantiate(loadPage("../pages/child-memory/index"));
      page.onLoad({ minorId: "person_child", name: "豆豆" });
      await page.refresh();
      assert.equal(page.data.activeTab, "recent");
      assert.equal(page.data.summaries.length, 1);
      assert.match(page.data.summaries[0].dateLabel, /10 月 13 日/);
      assert.equal(page.data.archiveConsent.active, false);
      assert.equal(page.data.archiveFlat.length, 0);
      assert.equal(page.data.privacyLine, childMemory.PRIVACY_LINE);

      page.askDeleteSummary({ currentTarget: { dataset: { recordId: "sum_1" } } });
      assert.equal(page.data.confirm.body, childMemory.DELETE_CONFIRM);
      assert.equal(page.data.confirm.quote, "孩子今天聊了小狗和画画。");
      assert.equal(page.data.deleteKeepLabel, childMemory.DELETE_KEEP);
      await page.runConfirm();
      assert.equal(page.data.summaries.length, 0);
      assert.equal(page.data.confirm, null);

      summaries = [
        {
          record_id: "sum_2",
          text: "孩子问起恐龙。",
          created_at: "2026-10-12T09:00:00+00:00",
        },
      ];
      await page.refresh();
      page.askClearSummaries();
      await page.runConfirm();
      assert.equal(page.data.statusBanner, childMemory.CLEARED_TOAST);
      assert.equal(page.data.summaries.length, 0);
    } finally {
      restore();
    }
  });
});

test("archive tab hides items without consent and confirms turn-off copy", async () => {
  await withWx(async () => {
    let consentActive = true;
    const restore = stubApi({
      getChildSessionSummaries: async () => ({
        privacy: { contains_transcript: false },
        items: [],
      }),
      getPersonConsents: async () => ({
        items: consentActive
          ? [
              {
                consent_kind: "child_archive",
                consent_id: "c_archive",
                active: true,
                granted_at: "2026-10-12T00:00:00+00:00",
              },
            ]
          : [],
      }),
      getChildArchive: async () => ({
        items: [
          {
            record_id: "arc_1",
            field: "preference",
            text: "恐龙",
            created_at: "2026-10-10T00:00:00+00:00",
            last_mentioned_at: "2026-10-12T08:00:00+00:00",
          },
        ],
      }),
      touchChildActivity: async () => ({}),
      revokePersonConsent: async () => {
        consentActive = false;
        return {};
      },
    });
    try {
      const page = instantiate(loadPage("../pages/child-memory/index"));
      page.onLoad({ minorId: "person_child", name: "豆豆" });
      await page.refresh();
      page.showArchive();
      assert.equal(page.data.activeTab, "archive");
      assert.equal(page.data.archiveConsent.active, true);
      assert.equal(page.data.archiveGroups[0].label, "兴趣");
      assert.equal(page.data.archiveGroups[0].items[0].text, "恐龙");

      await page.onArchiveConsentChange({ detail: { value: false } });
      assert.equal(page.data.confirm.body, childMemory.CONSENT_OFF_DIALOG);
      await page.runConfirm();
      assert.equal(page.data.archiveConsent.active, false);
      assert.equal(page.data.archiveFlat.length, 0);
    } finally {
      restore();
    }
  });
});
