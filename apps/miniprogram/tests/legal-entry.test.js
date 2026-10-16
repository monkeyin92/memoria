const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");
const { EFFECTIVE_ON, releaseChip } = require("../utils/legal-release");
const { AGREEMENT_PENDING, presentUserAgreement } = require("../utils/legal-doc");

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<style>[\s\S]*?<\/style>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function navigations(harness) {
  return harness.wxCalls
    .filter((call) => call.name === "navigateTo" || call.name === "redirectTo" || call.name === "reLaunch")
    .map((call) => call.args[0] && call.args[0].url);
}

test("the agreement file is packed even though only a runtime path names it", () => {
  const project = JSON.parse(fs.readFileSync(path.join(__dirname, "../project.config.example.json"), "utf8"));
  assert.ok(
    (project.packOptions.include || []).some((entry) => entry.type === "file" && entry.value === "legal/user-agreement.md"),
  );
});

test("an empty effective date stays 内测版 and a given date is shown as given", () => {
  assert.equal(EFFECTIVE_ON, "");
  assert.equal(releaseChip(""), "内测版");
  assert.equal(releaseChip("  "), "内测版");
  assert.equal(releaseChip("2026年4月1日"), "内测版 · 2026年4月1日生效");
});

const READY_AGREEMENT = "# 用户协议\n\n## 正文\n\n甲。\n";
const MARKED_AGREEMENT = "# 用户协议\n\n## 正文\n\n<!-- agreement: pending -->\n\n这是一段已经写好的协议正文，用来确认占位标记还在时不能勾选。\n";

test("consent links open their documents, and a finished agreement can be ticked before those documents are opened", async () => {
  const harness = loadPage("pages/auth/index", {
    ...fx.guest(),
    files: { "legal/user-agreement.md": READY_AGREEMENT },
  });
  await harness.run({ redirect: "/pages/home/index", reason: "view_dashboard" });
  try {
    const html = renderPage("pages/auth/index", harness.page.data);
    assert.match(html, /<text class="legal-link" data-tap>《隐私保护指引》<\/text>/);
    assert.match(html, /<text class="legal-link" data-tap>《监护人同意书》<\/text>/);
    harness.page.openPrivacy();
    assert.equal(harness.wxCalls.filter((call) => call.name === "openPrivacyContract").length, 1);
    harness.page.openGuardianConsent();
    assert.deepEqual(
      harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0]),
      [{ url: "/pages/guardian-consent/index" }],
    );
    assert.equal(harness.page.data.consented, false);
    assert.equal(harness.page.data.agreementHint, "");
    assert.doesNotMatch(html, /class="auth-consent auth-consent-locked"/);
    harness.page.toggleConsent();
    assert.equal(harness.page.data.consented, true);
    assert.equal(harness.page.data.releaseChip, "内测版");
    assert.doesNotMatch(visibleText(html), /草稿/);
  } finally {
    harness.restore();
  }
});

test("a pending or missing agreement locks the consent tick and shows the exact hint", async () => {
  assert.equal(presentUserAgreement(MARKED_AGREEMENT).pending, true);
  assert.equal(presentUserAgreement("").pending, true);
  const cases = [
    ["legal/user-agreement.md", MARKED_AGREEMENT],
    ["legal/user-agreement.md", ""],
  ];
  for (const [file, contents] of cases) {
    const harness = loadPage("pages/auth/index", { ...fx.guest(), files: { [file]: contents } });
    await harness.run({ reason: "view_dashboard" });
    try {
      const html = renderPage("pages/auth/index", harness.page.data);
      const text = visibleText(html);
      assert.match(html, /class="auth-consent auth-consent-locked"/);
      assert.match(text, /用户协议正在定稿，定稿后才能勾选同意/);
      assert.equal(harness.page.data.consented, false);
      harness.page.toggleConsent();
      assert.equal(harness.page.data.consented, false);
      assert.doesNotMatch(text, /草稿/);
    } finally {
      harness.restore();
    }
  }
});

test("login titles open the agreement and the privacy notice without a login redirect", async () => {
  const home = loadPage("pages/home/index", fx.guest());
  await home.run();
  try {
    const html = renderPage("pages/home/index", home.page.data);
    assert.match(html, /<text class="legal-link" data-tap>《用户协议》<\/text>/);
    assert.match(html, /<text class="legal-link" data-tap>《隐私说明》<\/text>/);
    home.page.openUserAgreement();
    home.page.openPrivacyNotice();
    assert.deepEqual(navigations(home), ["/pages/user-agreement/index", "/pages/privacy/index"]);
  } finally {
    home.restore();
  }

  const privacy = loadPage("pages/privacy/index", fx.guest());
  await privacy.run();
  try {
    assert.deepEqual(navigations(privacy), []);
    const text = visibleText(renderPage("pages/privacy/index", privacy.page.data));
    assert.match(text, /开启后，机器人对话的原始语音会保存在你的账号下，直到你撤回授权。撤回时服务端会删除这些录音。/);
    assert.equal(privacy.page.data.guest, true);
    assert.doesNotMatch(text, /草稿/);
    assert.doesNotMatch(text, /明确授权原始语音归档/);
  } finally {
    privacy.restore();
  }
});

test("the user agreement page shows the document body, or a pending notice when that body is empty", async () => {
  const pending = loadPage("pages/user-agreement/index", {
    ...fx.guest(),
    files: { "legal/user-agreement.md": MARKED_AGREEMENT },
  });
  await pending.run();
  try {
    assert.deepEqual(presentUserAgreement(MARKED_AGREEMENT), {
      pending: true,
      notice: AGREEMENT_PENDING,
      paragraphs: [],
    });
    assert.equal(pending.page.data.notice, "用户协议正在定稿");
    const text = visibleText(renderPage("pages/user-agreement/index", pending.page.data));
    assert.match(text, /^用户协议 用户协议正在定稿$/);
    assert.doesNotMatch(text, /草稿/);
    assert.doesNotMatch(text, /agreement: pending/);
  } finally {
    pending.restore();
  }

  const filled = "# 用户协议\n\n## 正文\n\n解析示例甲。\n\n解析示例乙。\n";
  const page = loadPage("pages/user-agreement/index", {
    ...fx.guest(),
    files: { "legal/user-agreement.md": filled },
  });
  await page.run();
  try {
    const text = visibleText(renderPage("pages/user-agreement/index", page.page.data));
    assert.equal(text, "用户协议 解析示例甲。 解析示例乙。");
    assert.doesNotMatch(text, /用户协议正在定稿/);
  } finally {
    page.restore();
  }
});

test("parent-visible login and consent surfaces do not say 草稿", async () => {
  const surfaces = [
    ["pages/auth/index", fx.guest(), { reason: "view_dashboard" }],
    ["pages/guardian-consent/index", fx.guest(), {}],
    ["pages/home/index", fx.guest(), {}],
    ["pages/privacy/index", fx.guest(), {}],
    ["pages/user-agreement/index", fx.guest(), {}],
  ];
  for (const [page, harnessOptions, query] of surfaces) {
    const harness = loadPage(page, harnessOptions);
    await harness.run(query);
    try {
      const text = visibleText(renderPage(page, harness.page.data));
      assert.equal(text.includes("草稿"), false, page);
      if (page === "pages/guardian-consent/index") assert.match(text, /^内测版 监护人同意书 /);
      if (page === "pages/auth/index") assert.match(text, /^内测版 /);
    } finally {
      harness.restore();
    }
  }
});
