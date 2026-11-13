const assert = require("node:assert/strict");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const CHILD = { minorId: "p_child", name: "乐乐" };

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openChildMemory(overrides, query = CHILD) {
  const harness = loadPage("pages/child-memory/index", fx.authed(overrides));
  await harness.run(query);
  return harness;
}

test("the archive leads the page with the design's three labels and footer, summaries follow below", async () => {
  const harness = await openChildMemory(fx.archiveApi);
  try {
    const html = renderPage("pages/child-memory/index", harness.page.data, { tabBar: false });
    const text = visibleText(html);
    assert.match(text, /^长期记住的事 乐乐的记忆 机器人长期只记住这三件事/);
    assert.match(text, /称呼[\s\S]*乐乐[\s\S]*孩子喜欢被这样叫/);
    assert.match(text, /喜好[\s\S]*恐龙[\s\S]*画画[\s\S]*聊天时会提到/);
    assert.match(text, /宠物名[\s\S]*小乐宝[\s\S]*讲故事时可能出现/);
    assert.match(text, /除这三项外，不长期保存其他信息。 长期记忆[\s\S]*清空长期记忆 近期摘要 讲了/);
    assert.match(text, /清空全部摘要/);
    assert.match(html, /class="cm-delete"/);
    assert.match(text, /家长已确认/);
    assert.match(text, /这条不对/);
    assert.match(text, /历史变更/);
    assert.match(text, /撤回/);
    assert.doesNotMatch(text, /兴趣|小名|共 \d+ 条/);
  } finally {
    harness.restore();
  }
});

test("archive lifecycle rows bind status and the review action set", async () => {
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({
      items: [
        {
          record_id: "auto-1",
          field: "preference",
          text: "喜欢恐龙",
          display_lifecycle: "auto_pending",
          provenance: "auto_extract",
        },
        {
          record_id: "pend-1",
          field: "appellation",
          text: "乐乐",
          display_lifecycle: "pending_confirm",
        },
        {
          record_id: "ok-1",
          field: "pet_name",
          text: "小乐宝",
          display_lifecycle: "parent_confirmed",
        },
        {
          record_id: "out-1",
          field: "preference",
          text: "不喜欢菠菜",
          display_lifecycle: "withdrawn",
        },
        {
          record_id: "exp-1",
          field: "preference",
          text: "旧喜好",
          display_lifecycle: "expired",
        },
      ],
    }),
  });
  try {
    const html = renderPage("pages/child-memory/index", harness.page.data, {
      tabBar: false,
      eventHandlers: true,
    });
    const text = visibleText(html);
    assert.match(text, /自动提取·待确认/);
    assert.match(text, /待家长确认/);
    assert.match(text, /家长已确认/);
    assert.match(text, /已撤回/);
    assert.match(text, /已过期/);
    assert.match(text, /确认这条记忆/);
    assert.match(text, /这条不对/);
    assert.match(text, /历史变更/);
    assert.match(text, /撤回/);
    assert.match(html, /data-handler="askConfirmArchive"[^>]*data-record-id="auto-1"|data-record-id="auto-1"[^>]*data-handler="askConfirmArchive"/);
    assert.match(html, /data-handler="askConfirmArchive"[^>]*data-record-id="pend-1"|data-record-id="pend-1"[^>]*data-handler="askConfirmArchive"/);
    assert.match(html, /data-handler="askCorrectArchive"[^>]*data-record-id="ok-1"|data-record-id="ok-1"[^>]*data-handler="askCorrectArchive"/);
    assert.doesNotMatch(html, /data-handler="askHistoryChange"[^>]*data-record-id="ok-1"|data-record-id="ok-1"[^>]*data-handler="askHistoryChange"/);
    assert.match(html, /data-handler="askWithdrawArchive"[^>]*data-record-id="ok-1"|data-record-id="ok-1"[^>]*data-handler="askWithdrawArchive"/);
    assert.match(html, /data-handler="askDeleteArchive"[^>]*data-record-id="ok-1"|data-record-id="ok-1"[^>]*data-handler="askDeleteArchive"/);
    assert.match(html, /data-handler="askHistoryChange"[^>]*data-record-id="out-1"|data-record-id="out-1"[^>]*data-handler="askHistoryChange"/);
    assert.doesNotMatch(html, /data-handler="askConfirmArchive"[^>]*data-record-id="out-1"|data-record-id="out-1"[^>]*data-handler="askConfirmArchive"/);
    assert.doesNotMatch(html, /data-handler="askCorrectArchive"[^>]*data-record-id="out-1"|data-record-id="out-1"[^>]*data-handler="askCorrectArchive"/);
    assert.doesNotMatch(html, /data-handler="askWithdrawArchive"[^>]*data-record-id="out-1"|data-record-id="out-1"[^>]*data-handler="askWithdrawArchive"/);
    assert.doesNotMatch(html, /data-handler="askDeleteArchive"[^>]*data-record-id="out-1"|data-record-id="out-1"[^>]*data-handler="askDeleteArchive"/);
    assert.match(html, /data-handler="askConfirmArchive"[^>]*data-record-id="exp-1"|data-record-id="exp-1"[^>]*data-handler="askConfirmArchive"/);
    assert.match(html, /data-handler="askDeleteArchive"[^>]*data-record-id="exp-1"|data-record-id="exp-1"[^>]*data-handler="askDeleteArchive"/);
    harness.page.askConfirmArchive({ currentTarget: { dataset: { recordId: "auto-1" } } });
    assert.equal(harness.page.data.confirm.kind, "confirm-archive");
    assert.equal(harness.page.data.confirm.title, "确认这条记忆？");
    harness.page.closeConfirm();
    harness.page.askHistoryChange({ currentTarget: { dataset: { recordId: "out-1" } } });
    assert.equal(harness.page.data.confirm.kind, "correct-archive");
    assert.equal(harness.page.data.confirm.historyChange, true);
  } finally {
    harness.restore();
  }
});

test("correction sheet accepts edited text and sends it to the review API", async () => {
  const reviews = [];
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({
      items: [{ record_id: "pref-1", field: "preference", text: "恐龙", display_lifecycle: "parent_confirmed" }],
    }),
    reviewChildArchiveFact: async (minorId, recordId, body) => {
      reviews.push({ minorId, recordId, body });
      return { new_record_id: "pref-2", audit_event_id: "audit-1" };
    },
  });
  try {
    harness.page.askCorrectArchive({ currentTarget: { dataset: { recordId: "pref-1" } } });
    assert.equal(harness.page.data.confirm.draftText, "恐龙");
    const html = renderPage("pages/child-memory/index", harness.page.data, {
      tabBar: false,
      eventHandlers: true,
    });
    assert.match(html, /<textarea[^>]*class="cm-sheet-input"/);
    assert.match(html, /placeholder="请家长输入正确内容"/);
    harness.page.onConfirmInput({ detail: { value: "喜欢恐龙" } });
    await harness.page.runConfirm();

    assert.deepEqual(reviews, [
      {
        minorId: "p_child",
        recordId: "pref-1",
        body: {
          action: "correct",
          text: "喜欢恐龙",
          polarity: undefined,
          history_change: false,
        },
      },
    ]);
    assert.equal(harness.page.data.confirm, null);
  } finally {
    harness.restore();
  }
});

test("correction blocks empty and unchanged drafts without calling the API", async () => {
  const reviews = [];
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({
      items: [{ record_id: "pet-1", field: "pet_name", text: "小乐宝", display_lifecycle: "parent_confirmed" }],
    }),
    reviewChildArchiveFact: async (...args) => {
      reviews.push(args);
      return {};
    },
  });
  try {
    harness.page.askCorrectArchive({ currentTarget: { dataset: { recordId: "pet-1" } } });
    harness.page.onConfirmInput({ detail: { value: "   " } });
    await harness.page.runConfirm();
    assert.equal(reviews.length, 0);
    assert.equal(harness.page.data.confirm.error, "请输入正确内容后再提交。");

    harness.page.onConfirmInput({ detail: { value: "小乐宝" } });
    await harness.page.runConfirm();
    assert.equal(reviews.length, 0);
    assert.equal(harness.page.data.confirm.error, "请修改内容后再提交。");
    assert.equal(harness.page.data.working, false);
  } finally {
    harness.restore();
  }
});

test("history change auto-drafts only preference semantics and keeps API errors in the sheet", async () => {
  let shouldFail = true;
  const reviews = [];
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({
      items: [
        { record_id: "pref-1", field: "preference", text: "喜欢恐龙", display_lifecycle: "parent_confirmed" },
        { record_id: "app-1", field: "appellation", text: "乐乐", display_lifecycle: "parent_confirmed" },
        { record_id: "pet-1", field: "pet_name", text: "小乐宝", display_lifecycle: "parent_confirmed" },
      ],
    }),
    reviewChildArchiveFact: async (minorId, recordId, body) => {
      reviews.push({ minorId, recordId, body });
      if (shouldFail) throw new Error("保存失败，请稍后重试。");
      return { new_record_id: "pref-2", audit_event_id: "audit-2" };
    },
  });
  try {
    const html = renderPage("pages/child-memory/index", harness.page.data, {
      tabBar: false,
      eventHandlers: true,
    });
    assert.match(html, /data-handler="askHistoryChange"[^>]*data-record-id="pref-1"|data-record-id="pref-1"[^>]*data-handler="askHistoryChange"/);
    assert.doesNotMatch(html, /data-handler="askHistoryChange"[^>]*data-record-id="app-1"|data-record-id="app-1"[^>]*data-handler="askHistoryChange"/);
    assert.doesNotMatch(html, /data-handler="askHistoryChange"[^>]*data-record-id="pet-1"|data-record-id="pet-1"[^>]*data-handler="askHistoryChange"/);

    harness.page.askHistoryChange({ currentTarget: { dataset: { recordId: "pref-1" } } });
    assert.equal(harness.page.data.confirm.draftText, "不喜欢恐龙");
    harness.page.onConfirmInput({ detail: { value: "喜欢海苔" } });
    await harness.page.runConfirm();
    assert.equal(harness.page.data.confirm.error, "保存失败，请稍后重试。");
    assert.equal(harness.page.data.error, "");
    assert.match(visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false })), /保存失败，请稍后重试。/);

    shouldFail = false;
    await harness.page.runConfirm();
    assert.equal(harness.page.data.confirm, null);
    assert.equal(reviews.length, 2);
    assert.equal(reviews[1].body.text, "喜欢海苔");
    assert.equal(reviews[1].body.polarity, "positive");

    harness.page.askHistoryChange({ currentTarget: { dataset: { recordId: "pet-1" } } });
    assert.equal(harness.page.data.confirm.historyChange, false);
    assert.equal(harness.page.data.confirm.draftText, "小乐宝");
  } finally {
    harness.restore();
  }
});

test("an open long-term memory shows each fact on its own row", async () => {
  const harness = await openChildMemory(fx.archiveApi);
  try {
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.match(
      text,
      /称呼[\s\S]*乐乐[\s\S]*孩子喜欢被这样叫[\s\S]*喜好[\s\S]*恐龙[\s\S]*画画[\s\S]*聊天时会提到[\s\S]*宠物名[\s\S]*小乐宝[\s\S]*讲故事时可能出现[\s\S]*除这三项外，不长期保存其他信息。/,
    );
    assert.doesNotMatch(text.split("近期摘要")[0], /10 月/);
  } finally {
    harness.restore();
  }
});

test("a closed long-term memory shows an empty state and does not list facts", async () => {
  let archiveReads = 0;
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getPersonConsents: async () => ({
      items: [{ consent_kind: "child_archive", consent_id: "c_archive", active: false }],
    }),
    getChildArchive: async () => {
      archiveReads += 1;
      return { items: fx.ARCHIVE };
    },
  });
  try {
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.equal(archiveReads, 0);
    assert.equal(harness.page.data.archiveConsent.active, false);
    assert.match(text, /长期记忆未开启/);
    assert.match(text, /已经记下的内容还在/);
    assert.doesNotMatch(text.split("近期摘要")[0], /恐龙|画画|小乐宝|孩子喜欢被这样叫/);
  } finally {
    harness.restore();
  }
});

test("with no summaries yet the section explains when they appear", async () => {
  const harness = await openChildMemory({ ...fx.archiveApi, ...fx.emptyMemoryApi });
  try {
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, / 近期摘要 Memoria还没有记住乐乐的事 还没有记忆。孩子和 Memoria 聊过之后，这里会出现摘要。 孩子说的原话/);
  } finally {
    harness.restore();
  }
});

test("each fact can be deleted and the archive can be cleared, both only after confirm", async () => {
  let archive = fx.ARCHIVE.map((item) => ({ ...item }));
  const deleted = [];
  const cleared = [];
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({ items: archive }),
    deleteChildArchiveFact: async (minorId, recordId) => {
      deleted.push([minorId, recordId]);
      archive = archive.filter((item) => item.record_id !== recordId);
      return { deleted: true };
    },
    clearChildArchive: async (minorId) => {
      cleared.push(minorId);
      archive = [];
      return { deleted: archive.length };
    },
  });
  try {
    const html = renderPage("pages/child-memory/index", harness.page.data, { tabBar: false });
    assert.match(html, /data-record-id="a2"/);
    assert.match(html, /data-record-id="a3"/);
    assert.match(html, /清空长期记忆/);
    harness.page.askDeleteArchive({ currentTarget: { dataset: { recordId: "a2" } } });
    assert.equal(deleted.length, 0);
    assert.equal(harness.page.data.confirm.title, "删除这条记忆？");
    harness.page.closeConfirm();
    harness.page.askClearArchive();
    assert.equal(cleared.length, 0);
    assert.equal(harness.page.data.confirm.title, "清空长期记忆？");
    await harness.page.runConfirm();
    assert.deepEqual(cleared, ["p_child"]);
  } finally {
    harness.restore();
  }
});

test("deleting a fact from the archive card removes it on the server and from the page", async () => {
  let archive = [
    { record_id: "a2", field: "preference", text: "恐龙", created_at: "2026-10-14T03:00:00+00:00" },
    { record_id: "a3", field: "preference", text: "画画", created_at: "2026-10-13T03:00:00+00:00" },
  ];
  const deleted = [];
  const harness = await openChildMemory({
    ...fx.archiveApi,
    getChildArchive: async () => ({ items: archive }),
    deleteChildArchiveFact: async (minorId, recordId) => {
      deleted.push([minorId, recordId]);
      archive = archive.filter((item) => item.record_id !== recordId);
      return { deleted: true };
    },
  });
  try {
    harness.page.askDeleteArchive({ currentTarget: { dataset: { recordId: "a2" } } });
    assert.equal(harness.page.data.confirm.title, "删除这条记忆？");
    assert.equal(harness.page.data.confirm.quote, "恐龙");
    await harness.page.runConfirm();
    assert.deepEqual(deleted, [["p_child", "a2"]]);
    assert.deepEqual(
      harness.page.data.archiveGroups.map((group) => [group.label, group.items.map((item) => item.text)]),
      [["喜好", ["画画"]]],
    );
  } finally {
    harness.restore();
  }
});

test("opening from the home preview scrolls once to 近期摘要", async () => {
  const harness = await openChildMemory(fx.archiveApi, { ...CHILD, section: "recent" });
  try {
    const scrolls = () => harness.wxCalls.filter((call) => call.name === "pageScrollTo").map((call) => call.args[0]);
    assert.deepEqual(scrolls(), [{ selector: "#cm-recent", duration: 0 }]);
    await harness.page.refresh();
    assert.equal(scrolls().length, 1);
  } finally {
    harness.restore();
  }
});

test("opening from anywhere else stays at the archive", async () => {
  const harness = await openChildMemory(fx.archiveApi);
  try {
    assert.deepEqual(harness.wxCalls.filter((call) => call.name === "pageScrollTo"), []);
  } finally {
    harness.restore();
  }
});

test("a logged-in parent whose memory read is refused does not see the raw login line", async () => {
  const refused = new Error("请先使用微信登录。");
  refused.status = 401;
  const harness = await openChildMemory({
    getChildSessionSummaries: async () => {
      throw refused;
    },
  });
  try {
    assert.equal(harness.page.data.authenticated, false);
    assert.equal(harness.page.data.error, "");
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /请先登录 登录后即可打开孩子的记忆档案/);
    assert.doesNotMatch(text, /请先使用微信登录/);
  } finally {
    harness.restore();
  }
});

test("a guest return clears a stale login error instead of leaving it above the empty state", async () => {
  const options = fx.guest();
  options.api.restoreWechatIdentity = async () => {
    throw Object.assign(new Error("no session"), { code: "no_session" });
  };
  const harness = loadPage("pages/child-memory/index", options);
  try {
    await harness.page.onLoad(CHILD);
    harness.page.setData({
      error: "请先使用微信登录。",
      authenticated: true,
      loadState: "error",
    });
    await harness.page.onShow();
    assert.equal(harness.page.data.authenticated, false);
    assert.equal(harness.page.data.error, "");
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.match(text, /请先登录 登录后即可打开孩子的记忆档案/);
    assert.doesNotMatch(text, /请先使用微信登录/);
  } finally {
    harness.restore();
  }
});

test("the home memory card opens the summaries section", async () => {
  const harness = loadPage("pages/home/index", fx.authed());
  try {
    await harness.run();
    harness.page.setData({ childMemoryMinorId: "p_child", childMemoryName: "乐乐" });
    harness.page.openChildMemory();
    const nav = harness.wxCalls.find((call) => call.name === "navigateTo");
    assert.equal(nav.args[0].url, "/pages/child-memory/index?minorId=p_child&name=%E4%B9%90%E4%B9%90&section=recent");
  } finally {
    harness.restore();
  }
});

for (const outcome of ["resolve", "reject", "logout"]) {
  test(`late archive ${outcome} cannot replace the latest empty or guest view`, async () => {
    let resolveArchive, rejectArchive, started;
    const entered = new Promise((resolve) => { started = resolve; });
    const pending = new Promise((resolve, reject) => { resolveArchive = resolve; rejectArchive = reject; });
    let reads = 0;
    let epoch = 1;
    let clearAuth;
    const options = fx.authed({
      ...fx.archiveApi,
      currentAuthEpoch: () => epoch,
      isAuthEpochCurrent: (value) => value === epoch,
      getChildArchive: () => { reads++; if (reads === 1) { started(); return pending; } return Promise.resolve({ items: [] }); },
    });
    options.app = { subscribeAuthCleared: (callback) => { clearAuth = callback; return () => {}; } };
    const h = loadPage("pages/child-memory/index", options);
    try {
      h.page.onLoad(CHILD);
      h.page.setData({ authenticated: true });
      const first = h.page.refresh();
      await entered;
      await h.page.refresh();
      if (outcome === "logout") { epoch++; clearAuth(); }
      if (outcome === "reject") rejectArchive(Object.assign(new Error("expired"), { status: 401 }));
      else resolveArchive({ items: fx.ARCHIVE });
      await first;
      assert.deepEqual(h.page.data.archiveFlat, []);
      const text = visibleText(renderPage("pages/child-memory/index", h.page.data));
      assert.doesNotMatch(text.split("近期摘要")[0], /恐龙|小乐宝/);
      if (outcome === "logout") assert.match(text, /请先登录/);
      else { assert.equal(h.page.data.authenticated, true); assert.equal(h.page.data.loadState, "ready"); }
    } finally { h.restore(); }
  });
}
