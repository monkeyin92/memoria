const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function authApi({ restoreError = { code: "phone_authorization_required" }, loginError = null } = {}) {
  const logins = [];
  const api = {
    hasAuthenticatedSession: () => false,
    restoreWechatIdentity: async () => {
      throw Object.assign(new Error("需要手机号授权"), restoreError);
    },
    loginWithWechat: async (payload) => {
      logins.push(payload);
      if (loginError) throw new Error(loginError);
    },
    uploadWechatAvatar: async () => {},
  };
  return { api, logins };
}

async function openAuth(options) {
  const { api, logins } = authApi(options);
  const harness = loadPage("pages/auth/index", { api });
  await harness.run({ redirect: "/pages/home/index", reason: "view_dashboard" });
  const navigations = () =>
    harness.wxCalls.filter((call) => ["switchTab", "redirectTo", "navigateBack"].includes(call.name)).map((call) => [call.name, call.args[0]]);
  return { harness, page: harness.page, logins, navigations };
}

test("consent screen shows facts, guardian consent and both choices before any login field", async () => {
  const { harness } = await openAuth();
  try {
    const text = visibleText(renderPage("pages/auth/index", harness.page.data));
    assert.equal(
      text,
      "草稿 · 待律师审核 开始前， 请先确认这几件事 作为孩子的监护人，你需要知道我们实际会处理哪些信息。 " +
        "对话经过我们的服务器 孩子说的话会经服务器处理后再回复 保存对话摘要 按天查看，原话在入库前就去掉 " +
        "长期只记三项 孩子的称呼、喜好、宠物名 回应由 AI 生成 会明确标识；模型训练默认关闭 " +
        "我是孩子的监护人，已阅读并同意 《隐私保护指引》 同意并继续 暂不同意 登录后即可查看设备和今天记下的内容。 " +
        "已登录过的微信会自动恢复；首次登录需要授权手机号，只用于绑定账号，不会公开展示。 " +
        "手机不录音，机器人本身不存录音。使用人由设备绑定确定，不通过声音识别身份。",
    );
  } finally {
    harness.restore();
  }
});

test("同意并继续 stays disabled until the guardian ticks consent", async () => {
  const { harness, page } = await openAuth();
  try {
    const cta = () => renderPage("pages/auth/index", page.data).match(/<button[^>]*>同意并继续/)[0];
    assert.match(cta(), /disabled/);
    page.toggleConsent();
    assert.equal(page.data.consented, true);
    assert.doesNotMatch(cta(), /disabled/);
    page.toggleConsent();
    assert.equal(page.data.consented, false);
  } finally {
    harness.restore();
  }
});

test("暂不同意 leaves the page without logging in", async () => {
  const { harness, page, logins, navigations } = await openAuth();
  try {
    page.goBack();
    assert.deepEqual(navigations(), [["switchTab", { url: "/pages/home/index" }]]);
    assert.deepEqual(logins, []);
  } finally {
    harness.restore();
  }
});

test("a returning WeChat user logs in straight from the consent step", async () => {
  const { harness, page, logins, navigations } = await openAuth({ restoreError: { code: "network" } });
  try {
    assert.equal(page.data.needsSalutation, false);
    page.toggleConsent();
    await page.onGetPhoneNumber({ detail: { code: "phone-1" } });
    assert.deepEqual(logins, [{ phoneCode: "phone-1", displayName: "" }]);
    assert.deepEqual(navigations(), [["switchTab", { url: "/pages/home/index" }]]);
  } finally {
    harness.restore();
  }
});

test("first registration holds the phone code until the user says how to be addressed", async () => {
  const { harness, page, logins, navigations } = await openAuth();
  try {
    assert.equal(page.data.needsSalutation, true);
    page.toggleConsent();
    await page.onGetPhoneNumber({ detail: { code: "phone-2" } });
    assert.equal(page.data.step, "profile");
    assert.deepEqual(logins, []);
    const text = visibleText(renderPage("pages/auth/index", page.data));
    assert.match(text, /^最后一步 首次注册后，我会在自然的对话里这样称呼你。 选择头像 微信用户/);
    assert.match(text, /怎么称呼你？ \*[\s\S]*完成登录$/);

    await page.submitProfile();
    assert.equal(page.data.error, "先告诉我怎么称呼你吧。");
    assert.deepEqual(logins, []);

    page.onNicknameInput({ detail: { value: " 乐乐妈妈 " } });
    await page.submitProfile();
    assert.deepEqual(logins, [{ phoneCode: "phone-2", displayName: "乐乐妈妈" }]);
    assert.deepEqual(navigations(), [["switchTab", { url: "/pages/home/index" }]]);
  } finally {
    harness.restore();
  }
});

test("a failed registration returns to consent for a fresh phone authorization", async () => {
  const { harness, page, logins } = await openAuth({ loginError: "手机号已绑定其他账号" });
  try {
    page.toggleConsent();
    await page.onGetPhoneNumber({ detail: { code: "phone-3" } });
    page.onNicknameInput({ detail: { value: "乐乐妈妈" } });
    await page.submitProfile();
    assert.equal(page.data.step, "consent");
    assert.equal(page.data.error, "手机号已绑定其他账号");
    assert.equal(page.data.loading, false);
    await page.onGetPhoneNumber({ detail: { code: "phone-3b" } });
    assert.equal(page.data.step, "profile");
    assert.equal(page.data.nickname, "乐乐妈妈");
    await page.submitProfile();
    assert.deepEqual(logins.map((login) => login.phoneCode), ["phone-3", "phone-3b"]);
  } finally {
    harness.restore();
  }
});

test("the profile step's back button returns to consent and drops the held code", async () => {
  const { harness, page } = await openAuth();
  try {
    page.toggleConsent();
    await page.onGetPhoneNumber({ detail: { code: "phone-4" } });
    page.backToConsent();
    assert.equal(page.data.step, "consent");
    assert.equal(page.data.consented, true);
  } finally {
    harness.restore();
  }
});
