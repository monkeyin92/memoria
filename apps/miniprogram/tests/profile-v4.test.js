const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { ROOT, loadPage } = require("../scripts/mp-harness/runtime");
const { parseWxml, renderPage } = require("../scripts/mp-harness/render");
const fx = require("../scripts/mp-harness/fixtures");

const PAGE = "pages/profile/index";
const ABOUT_LEAD = "陪孩子聊天、讲故事的 AI 助手。";
const DEEPSEEK_LINE = "对话大模型：DeepSeek（备案号 Beijing-DeepseekChat-202404280016）";
const QWEN_FILING =
  "对话大模型：通义千问（网信算备330110507206401230035号，登记号 ZheJiang-TongYiQianWen-20230901）";

function aboutCard(harness) {
  const modalsBefore = callsTo(harness, "showModal").length;
  harness.page.openAbout();
  assert.equal(callsTo(harness, "showModal").length, modalsBefore);
  const html = renderPage(PAGE, harness.page.data, { tabBar: false });
  assert.match(html, /\.about-line\s*\{[^}]*text-align:\s*left/);
  assert.match(html, /<text class="modal-title">关于 Memoria<\/text>/);
  assert.match(html, /<button[^>]*about-dismiss[^>]*>知道了<\/button>/);
  return [...html.matchAll(/<text class="about-line">([^<]*)<\/text>/g)].map((match) => match[1]);
}

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

async function openProfile(options, beforeLoad) {
  const harness = loadPage(PAGE, options);
  beforeLoad?.();
  await harness.run();
  return harness;
}

function profileText(harness) {
  return visibleText(renderPage(PAGE, harness.page.data, { tabBar: false }));
}

function callsTo(harness, name) {
  return harness.wxCalls.filter((call) => call.name === name).map((call) => call.args[0]);
}

function textOf(node) {
  if (node.type === "text") return node.value;
  return (node.children || []).map(textOf).join("");
}

function findElements(node, predicate, found = []) {
  if (node.type === "el" && predicate(node)) found.push(node);
  for (const child of node.children || []) findElements(child, predicate, found);
  return found;
}

test("signed in, the page shows the account card, the robot card and the design's four menu rows", async () => {
  const harness = await openProfile(fx.authed());
  try {
    const text = profileText(harness);
    assert.match(
      text,
      /^我的 乐 乐乐妈妈 已绑定 1 台设备 孩子的星澜 已绑定设备 设备管理 授权与隐私说明 帮助与反馈 关于 账号 账号编号 /,
    );
    assert.match(text, / 永久注销 .* 开发版$/);
    assert.doesNotMatch(text, /自定义人格|你的日常角色|自动整理日常回顾|偏好/);
  } finally {
    harness.restore();
  }
});

test("returning to the tab keeps the robot's name on screen while the profile refreshes", async () => {
  let profileCalls = 0;
  let releaseProfile;
  const harness = await openProfile(fx.authed({
    getProfile: async () => {
      profileCalls += 1;
      if (profileCalls > 1) await new Promise((resolve) => { releaseProfile = resolve; });
      return { user_id: "u_parent", display_name: "乐乐妈妈", companion_id: "starlight" };
    },
  }));
  try {
    harness.page.onShow();
    await harness.settle();
    assert.equal(profileCalls, 2);
    assert.match(profileText(harness), /^我的 乐 乐乐妈妈 已绑定 1 台设备 孩子的星澜 已绑定设备 /);
    releaseProfile();
    await harness.settle();
    assert.match(profileText(harness), / 孩子的星澜 已绑定设备 /);
  } finally {
    harness.restore();
  }
});

test("a guest still gets privacy, feedback and about, and 微信登录 opens the login page", async () => {
  const options = fx.guest();
  options.api.restoreWechatIdentity = async () => {
    throw { code: "no_session" };
  };
  const harness = await openProfile(options);
  try {
    assert.equal(
      profileText(harness),
      "我的 设备与记录，登录后跟随账号。 登录后即可管理你的资料。未登录时不会读取资料、统计或回顾。 微信登录 授权与隐私说明 帮助与反馈 关于 开发版",
    );
    assert.deepEqual(aboutCard(harness), [ABOUT_LEAD, "开发版"]);
    harness.page.closeAbout();
    assert.equal(harness.page.data.showAbout, false);
    assert.doesNotMatch(profileText(harness), /关于 Memoria/);
    await harness.page.loginFromProfile();
    assert.deepEqual(callsTo(harness, "navigateTo"), [
      { url: "/pages/auth/index?redirect=%2Fpages%2Fhome%2Findex&reason=view_profile" },
    ]);
    assert.equal(harness.page.data.authenticated, false);
  } finally {
    harness.restore();
  }
});

test("帮助与反馈 is WeChat's native feedback button", () => {
  const root = parseWxml(fs.readFileSync(path.join(ROOT, PAGE + ".wxml"), "utf8"));
  const rows = findElements(root, (node) => textOf(node).trim() === "帮助与反馈" && node.attrs?.class === "list-row");
  assert.deepEqual(
    rows.map((node) => [node.name, node.attrs["open-type"]]),
    [["button", "feedback"]],
  );
});

test("关于 shows the release version, or the build channel before release, and no model filing the server did not report", async () => {
  const develop = await openProfile(fx.authed());
  try {
    assert.deepEqual(develop.page.data.chatLlmProviders, []);
    assert.deepEqual(aboutCard(develop), [ABOUT_LEAD, "开发版"]);
  } finally {
    develop.restore();
  }

  const release = await openProfile(fx.authed(), () => {
    global.wx.getAccountInfoSync = () => ({ miniProgram: { envVersion: "release", version: "1.4.0" } });
  });
  try {
    assert.match(profileText(release), / 版本 1\.4\.0$/);
    assert.deepEqual(aboutCard(release), [ABOUT_LEAD, "版本 1.4.0"]);
  } finally {
    release.restore();
  }
});

test("chat_llm_providers empty, before the voice agent reports its models, shows no model filing", async () => {
  const harness = await openProfile(fx.authed({
    getDeliveredCapabilities: async () => ({ chat_llm_providers: [] }),
  }));
  try {
    assert.deepEqual(harness.page.data.chatLlmProviders, []);
    assert.deepEqual(aboutCard(harness), [ABOUT_LEAD, "开发版"]);
  } finally {
    harness.restore();
  }
});

test("chat_llm_providers deepseek keeps the DeepSeek filing", async () => {
  const harness = await openProfile(fx.authed({
    getDeliveredCapabilities: async () => ({ chat_llm_providers: ["deepseek"] }),
  }));
  try {
    assert.deepEqual(harness.page.data.chatLlmProviders, ["deepseek"]);
    assert.deepEqual(aboutCard(harness), [ABOUT_LEAD, DEEPSEEK_LINE, "开发版"]);
  } finally {
    harness.restore();
  }
});

test("chat_llm_providers qwen lists only the 通义千问 filing", async () => {
  const harness = await openProfile(fx.authed({
    getDeliveredCapabilities: async () => ({ chat_llm_providers: ["qwen"] }),
  }));
  try {
    assert.deepEqual(harness.page.data.chatLlmProviders, ["qwen"]);
    assert.deepEqual(aboutCard(harness), [ABOUT_LEAD, QWEN_FILING, "开发版"]);
  } finally {
    harness.restore();
  }
});

test("chat_llm_providers deepseek and qwen discloses both filings", async () => {
  const harness = await openProfile(fx.authed({
    getDeliveredCapabilities: async () => ({ chat_llm_providers: ["deepseek", "qwen"] }),
  }));
  try {
    assert.deepEqual(harness.page.data.chatLlmProviders, ["deepseek", "qwen"]);
    const lines = aboutCard(harness);
    assert.deepEqual(lines, [
      ABOUT_LEAD,
      "对话可能由 DeepSeek 或阿里云通义千问生成",
      DEEPSEEK_LINE,
      QWEN_FILING,
      "开发版",
    ]);
    assert.doesNotMatch(lines.join("\n"), /备用/);
  } finally {
    harness.restore();
  }
});

test("授权与隐私说明 opens the consent page when raw-audio consent is open, the WeChat privacy guideline otherwise", async () => {
  const closed = await openProfile(fx.authed());
  try {
    assert.equal(closed.page.data.rawVoiceEntryAllowed, false);
    closed.page.openPrivacy();
    await closed.settle();
    assert.equal(callsTo(closed, "openPrivacyContract").length, 1);
    assert.deepEqual(callsTo(closed, "showModal").map((options) => options.title), ["隐私说明"]);
    assert.deepEqual(callsTo(closed, "navigateTo"), []);
    assert.deepEqual(callsTo(closed, "showToast"), []);
  } finally {
    closed.restore();
  }

  const open = await openProfile(fx.authed({ getRuntimeProfile: async () => ({ ...fx.RUNTIME_PROFILE, capabilities: [] }) }));
  try {
    assert.equal(open.page.data.rawVoiceEntryAllowed, true);
    open.page.openPrivacy();
    assert.deepEqual(callsTo(open, "navigateTo"), [{ url: "/pages/privacy/index" }]);
    assert.deepEqual(callsTo(open, "openPrivacyContract"), []);
  } finally {
    open.restore();
  }
});
