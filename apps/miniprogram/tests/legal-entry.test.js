const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");
const { EFFECTIVE_ON, consentLocked, releaseChip } = require("../utils/legal-release");
const { consentStamp, CHILD_INFO_CONSENT_KEY } = require("../utils/child-consent");
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

test("the shipped agreement carries its effective date", () => {
  assert.equal(EFFECTIVE_ON, "2026年10月24日");
  assert.equal(releaseChip(), "内测版 · 2026年10月24日生效");
  assert.equal(releaseChip(""), "内测版");
  assert.equal(releaseChip("  "), "内测版");
  assert.equal(releaseChip("2026年4月1日"), "内测版 · 2026年4月1日生效");
});

const READY_AGREEMENT = "# 用户协议\n\n## 正文\n\n甲。\n";
const MARKED_AGREEMENT = "# 用户协议\n\n## 正文\n\n<!-- agreement: pending -->\n\n这是一段已经写好的协议正文，用来确认占位标记还在时不能勾选。\n";

test("consent stays locked unless the agreement body and the effective date are both present", async () => {
  const hint = "用户协议正在定稿，定稿后才能勾选同意";
  const cases = [
    { name: "只缺正文", agreement: MARKED_AGREEMENT, effectiveOn: "2026年4月1日", locked: true },
    { name: "正文缺失", agreement: "", effectiveOn: "2026年4月1日", locked: true },
    { name: "只缺日期", agreement: READY_AGREEMENT, effectiveOn: "", locked: true },
    { name: "日期只有空白", agreement: READY_AGREEMENT, effectiveOn: "  ", locked: true },
    { name: "都缺", agreement: MARKED_AGREEMENT, effectiveOn: "", locked: true },
    { name: "都齐", agreement: READY_AGREEMENT, effectiveOn: "2026年4月1日", locked: false },
  ];
  for (const item of cases) {
    const harness = loadPage("pages/child-consent/index", {
      ...fx.guest(),
      files: { "legal/user-agreement.md": item.agreement },
      effectiveOn: item.effectiveOn,
    });
    await harness.run({ reason: "view_dashboard", skip_restore: "1", debug: "1", consent: "1" });
    try {
      const html = renderPage("pages/child-consent/index", harness.page.data);
      const text = visibleText(html);
      assert.equal(harness.page.data.consented, false, item.name);
      harness.page.toggleConsent();
      if (item.locked) {
        assert.match(html, /class="auth-consent auth-consent-locked"/, item.name);
        assert.match(text, new RegExp(hint), item.name);
        assert.equal(harness.page.data.consented, false, item.name);
      } else {
        assert.doesNotMatch(html, /class="auth-consent auth-consent-locked"/, item.name);
        assert.equal(harness.page.data.agreementHint, "", item.name);
        assert.doesNotMatch(text, new RegExp(hint), item.name);
        assert.equal(harness.page.data.consented, true, item.name);
        assert.equal(harness.page.data.releaseChip, "内测版 · 2026年4月1日生效");
      }
      assert.doesNotMatch(text, /草稿/, item.name);
    } finally {
      harness.restore();
    }
  }
});

test("consent links open their documents before the guardian ticks a finished release", async () => {
  const harness = loadPage("pages/child-consent/index", {
    ...fx.guest(),
    files: { "legal/user-agreement.md": READY_AGREEMENT },
    effectiveOn: "2026年4月1日",
  });
  await harness.run({ redirect: "/pages/home/index" });
  try {
    const html = renderPage("pages/child-consent/index", harness.page.data);
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
    harness.page.toggleConsent();
    assert.equal(harness.page.data.consented, true);
  } finally {
    harness.restore();
  }
});

test("login titles open the agreement and the privacy notice without a login redirect", async () => {
  const home = loadPage("pages/home/index", fx.guest());
  await home.run();
  try {
    const html = renderPage("pages/home/index", home.page.data);
    assert.match(html, /<text class="legal-link" data-tap data-id="terms">《用户服务协议》<\/text>/);
    assert.match(html, /<text class="legal-link" data-tap data-id="privacy">《隐私政策》<\/text>/);
    home.page.openGuestAgreement({ currentTarget: { dataset: { id: "terms" } } });
    home.page.openGuestAgreement({ currentTarget: { dataset: { id: "privacy" } } });
    assert.deepEqual(navigations(home), ["/pages/user-agreement/index", "/pages/privacy-policy/index"]);
  } finally {
    home.restore();
  }

  const privacy = loadPage("pages/privacy/index", fx.guest());
  await privacy.run();
  try {
    assert.deepEqual(navigations(privacy), []);
    const text = visibleText(renderPage("pages/privacy/index", privacy.page.data));
    assert.equal(privacy.page.data.guest, true);
    assert.doesNotMatch(text, /草稿/);
    assert.doesNotMatch(text, /原始语音归档|明确授权原始语音归档/);
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
    assert.match(text, /^用户服务协议 用户协议正在定稿$/);
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
    assert.equal(text, "用户服务协议 解析示例甲。 解析示例乙。");
    assert.doesNotMatch(text, /用户协议正在定稿/);
  } finally {
    page.restore();
  }
});

const SHIPPED_AGREEMENT_PARAGRAPHS = [
  "memoria 用户协议（内测版）",
  "欢迎使用 memoria。memoria 是给 3 到 10 岁孩子用的陪伴机器人，配合微信小程序使用，现在还在内测阶段。这份协议是你（孩子的父母或其他监护人）和 memoria 运营方之间关于使用 memoria 的约定，请先读完再勾选同意。和孩子个人信息有关的内容，包括收集哪些、保存多久，以《隐私保护指引》和《监护人同意书》为准。",
  "一、服务内容",
  "1. memoria 提供 AI 陪伴对话、讲故事、睡前音频等功能，你可以在小程序里绑定设备、查看提醒和回顾、管理孩子的数据。\n2. 和孩子对话的是人工智能，不是真人。你听到的声音由人工智能合成，不是真人在说话。离线故事和睡前音频的音频文件里带有 AI 生成标记。识别和生成回复时，孩子说的这一句会交给服务商处理。孩子的对话默认不用于模型训练。\n3. AI 生成的内容可能不准确、不完整，不能当作医疗、心理、教育或法律方面的专业意见。\n4. 内测期间功能会调整，具体以你实际看到的为准。",
  "二、监护人同意和监护人责任",
  "1. 未成年人使用 memoria，以及我们处理未成年人的个人信息，都要先取得父母或其他监护人的同意。未满 14 周岁和 14 到 17 周岁适用同一条规则。你勾选同意，就表示你是孩子的监护人，有权代孩子作出同意。\n2. memoria 不能替代你对孩子的照看。请你留意孩子的使用时长和状态，孩子使用时尽量有大人在旁边或能随时找到大人。\n3. 孩子聊到安全话题时，机器人会提醒孩子去找身边的大人，小程序里可能出现提醒。小程序里给你的提醒不包含孩子的原话。提醒不保证每次都能识别或及时送到，遇到紧急情况请直接拨打 110、120，心理援助可拨打 12356。",
  "三、账号",
  "1. 你用微信登录小程序，账号仅限你本人和家里的监护人使用，请保管好手机和微信账号。\n2. 一台设备绑定到一个家庭。设备转给别人前，请先解绑并删除孩子的数据。\n3. 发现账号被别人使用，请到小程序「我的」页点「帮助与反馈」告诉我们。",
  "四、禁止行为",
  "使用 memoria 时请不要：\n1. 用它做违法的事，或者引导它生成违法、暴力、色情、侵害未成年人的内容；\n2. 破解、反向工程、修改设备固件或服务器接口，绕过安全和防沉迷设置；\n3. 冒用他人身份绑定设备，或在没有监护权的情况下替别人的孩子作出同意；\n4. 把设备或服务转卖、出租，或用于商业目的；\n5. 干扰服务正常运行，比如批量请求、植入恶意代码。",
  "出现上述情况，我们可以暂停或终止对应账号的服务，并依法处理。",
  "五、内测性质和停止服务",
  "1. memoria 现在是内测版，可能有故障、中断或功能变动，我们会尽量修复，但不能保证服务一直不间断。\n2. 如果我们决定停止提供 memoria 服务，会至少提前 90 天在小程序里通知你。这段时间你可以在「家庭与监护」页导出孩子的授权和数据管理记录。这份导出不含对话原文。你也可以删除孩子的数据。停服后停止收集并删除孩子的个人信息。",
  "六、知识产权",
  "1. memoria 的软件、固件、界面设计、故事和儿歌文稿、合成音频等，权利归运营方或原权利人所有。\n2. 你可以在内测期间为家庭使用目的使用这些内容，不得复制、传播或用于商业用途。\n3. 你主动提交给我们的反馈和建议，我们可以用来改进产品，不会公开你和孩子的身份。",
  "七、免责和责任限制",
  "1. AI 生成内容可能出错，你根据这些内容做的决定需要自己判断。\n2. 因网络、第三方服务（如微信、模型服务商）故障，或不可抗力造成的服务中断，我们会尽快恢复，但在法律允许的范围内不承担由此产生的间接损失。\n3. 上面的约定不排除我们依法应当承担的责任，也不限制你依法享有的权利。因我们故意或重大过失造成你或孩子人身伤害、财产损失的，我们依法承担责任。",
  "八、协议变更",
  "1. 协议有变化时，我们会在小程序里明显提示，并写明新版本的生效日期。\n2. 涉及你权利义务的重要变更，我们会重新请你确认。你不同意的，可以停止使用并要求删除数据。",
  "九、联系方式",
  "运营方名称和联系方式：公司注册后补充。",
  "内测期间对协议有疑问，可以在小程序「我的」页点「帮助与反馈」告诉我们。这是微信自带的反馈功能，不经过 memoria 的服务器。",
  "投诉回复时限待定。",
];

test("the shipped user agreement page shows the dated body and opens the gate", async () => {
  const harness = loadPage("pages/user-agreement/index", fx.guest());
  await harness.run();
  try {
    assert.equal(harness.page.data.pending, false);
    assert.equal(harness.page.data.notice, "");
    assert.deepEqual(harness.page.data.paragraphs, SHIPPED_AGREEMENT_PARAGRAPHS);
    const text = visibleText(renderPage("pages/user-agreement/index", harness.page.data));
    assert.doesNotMatch(text, /EFFECTIVE_ON/);
    assert.doesNotMatch(text, /agreement: pending/);
    assert.doesNotMatch(text, /正在定稿/);
    assert.doesNotMatch(text, /草稿/);
    assert.doesNotMatch(text, /角标/);
    assert.doesNotMatch(text, /提示音/);
    assert.doesNotMatch(text, /2026-10-16/);
    assert.doesNotMatch(text, /隐私说明/);
    assert.doesNotMatch(text, /内测协议/);
    assert.doesNotMatch(text, /小程序里由 AI 生成的总结也会标明/);
    assert.doesNotMatch(text, /请尽快联系我们/);
    let cursor = 0;
    for (const paragraph of SHIPPED_AGREEMENT_PARAGRAPHS) {
      const needle = paragraph.replace(/\s+/g, " ").trim();
      const at = text.indexOf(needle, cursor);
      assert.notEqual(at, -1, needle);
      cursor = at + needle.length;
    }
    assert.equal(consentLocked(false, EFFECTIVE_ON), false);
  } finally {
    harness.restore();
  }
});

test("parent-visible login and consent surfaces do not say 草稿", async () => {
  const surfaces = [
    ["pages/auth/index", fx.guest(), { reason: "view_dashboard" }],
    ["pages/child-consent/index", fx.guest(), {}],
    ["pages/privacy-policy/index", fx.guest(), {}],
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
      if (page === "pages/guardian-consent/index") {
        assert.match(text, /^内测版 监护人同意书 /);
        assert.doesNotMatch(text, /内测版 · /);
      }
      if (page === "pages/child-consent/index") assert.match(text, /^内测版 · 2026年10月24日生效 /);
    } finally {
      harness.restore();
    }
  }
});

test("confirming stores the versions and a stored older version sends the guardian back", async () => {
  const harness = loadPage("pages/child-consent/index", {
    ...fx.guest(),
    files: { "legal/user-agreement.md": READY_AGREEMENT },
    effectiveOn: "2026年4月1日",
  });
  await harness.run({ redirect: "/pages/home/index" });
  try {
    harness.page.toggleConsent();
    harness.page.confirmChildConsent();
    const stored = harness.wxCalls.filter(
      (call) => call.name === "setStorageSync" && call.args[0] === CHILD_INFO_CONSENT_KEY,
    );
    assert.deepEqual(stored.map((call) => call.args[1]), [consentStamp()]);
  } finally {
    harness.restore();
  }

  const stale = loadPage("pages/bind/index", {
    ...fx.authed(),
    storage: { ...fx.STORAGE, [CHILD_INFO_CONSENT_KEY]: "accepted" },
  });
  stale.page._claimLoaded = true;
  await stale.page.onShow();
  try {
    assert.deepEqual(navigations(stale), ["/pages/child-consent/index?redirect=%2Fpages%2Fhome%2Findex"]);
  } finally {
    stale.restore();
  }

  const current = loadPage("pages/bind/index", fx.authed());
  await current.run();
  try {
    assert.deepEqual(navigations(current), []);
  } finally {
    current.restore();
  }
});
