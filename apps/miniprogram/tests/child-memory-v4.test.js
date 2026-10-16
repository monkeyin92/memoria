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
    const text = visibleText(renderPage("pages/child-memory/index", harness.page.data, { tabBar: false }));
    assert.match(
      text,
      /^长期记住的事 乐乐的记忆 机器人长期只记住这三件事，用来更自然地和孩子聊天。 称呼 乐乐 孩子喜欢被这样叫 喜好 恐龙 画画 聊天时会提到 宠物名 小乐宝 讲故事时可能出现 除这三项外，不长期保存其他信息。 长期记忆 开启后，Memoria 会记住孩子提到的称呼、喜好和宠物名。你可以随时关闭，关闭后不再记新的内容。 清空长期记忆 近期摘要 讲了/,
    );
    assert.doesNotMatch(text, /兴趣|小名|共 \d+ 条/);
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
      /称呼 乐乐 孩子喜欢被这样叫 喜好 恐龙 画画 聊天时会提到 宠物名 小乐宝 讲故事时可能出现 除这三项外，不长期保存其他信息。/,
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
    assert.match(text, / 近期摘要 星澜还没有记住乐乐的事 还没有记忆。孩子和 Memoria 聊过之后，这里会出现摘要。 孩子说的原话/);
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
