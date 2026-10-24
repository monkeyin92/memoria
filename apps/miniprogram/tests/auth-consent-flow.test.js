const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");
const { ACCOUNT_CONSENT_PROMPT } = require("../utils/guest-access");
const { CHILD_INFO_CONSENT_KEY } = require("../utils/child-consent");

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<style>[\s\S]*?<\/style>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function authApi({ loginError = null } = {}) {
  const logins = [];
  const api = {
    hasAuthenticatedSession: () => false,
    loginWithWechat: async (payload) => {
      logins.push(payload);
      if (loginError) throw new Error(loginError);
    },
  };
  return { api, logins };
}

async function openAuth(options) {
  const { api, logins } = authApi(options);
  const harness = loadPage("pages/auth/index", { api });
  await harness.run({ redirect: "/pages/home/index", reason: "view_dashboard" });
  const navigations = () =>
    harness.wxCalls
      .filter((call) => ["switchTab", "redirectTo", "navigateBack", "navigateTo"].includes(call.name))
      .map((call) => [call.name, call.args[0]]);
  return { harness, page: harness.page, logins, navigations };
}

test("the login checkbox starts unchecked and names both agreements", async () => {
  const { harness, page } = await openAuth();
  try {
    assert.equal(page.data.accountAgreed, false);
    const text = visibleText(renderPage("pages/auth/index", page.data));
    assert.match(text, /我已阅读并同意 《用户服务协议》 和 《隐私政策》/);
    assert.match(text, /登录/);
    assert.doesNotMatch(text, /选择头像|怎么称呼你|同意并继续/);
    const login = renderPage("pages/auth/index", page.data).match(/<button[^>]*>登录/)[0];
    assert.doesNotMatch(login, /disabled/);
    assert.doesNotMatch(login, /getPhoneNumber/);
  } finally {
    harness.restore();
  }
});

test("agreement titles open the full text and do not check the box", async () => {
  const { harness, page } = await openAuth();
  try {
    page.openUserAgreement();
    page.openPrivacyPolicy();
    assert.deepEqual(
      harness.wxCalls.filter((call) => call.name === "navigateTo").map((call) => call.args[0].url),
      ["/pages/user-agreement/index", "/pages/privacy-policy/index"],
    );
    page.onShow();
    assert.equal(page.data.accountAgreed, false);
  } finally {
    harness.restore();
  }
});

test("login while unchecked shows the prompt and sends no request", async () => {
  const { harness, page, logins } = await openAuth();
  try {
    await page.onLoginWithoutConsent();
    assert.equal(page.data.error, ACCOUNT_CONSENT_PROMPT);
    assert.equal(page.data.accountAgreed, false);
    assert.deepEqual(logins, []);
    assert.equal(harness.wxCalls.filter((call) => call.name === "login").length, 0);
    assert.equal(
      harness.wxCalls.filter((call) => call.name === "showToast").map((call) => call.args[0].title)[0],
      ACCOUNT_CONSENT_PROMPT,
    );
    await page.onGetPhoneNumber({ detail: { code: "phone-0" } });
    assert.deepEqual(logins, []);
    assert.equal(harness.wxCalls.filter((call) => call.name === "login").length, 0);
  } finally {
    harness.restore();
  }
});

test("a checked login sends only the phone code", async () => {
  const { harness, page, logins, navigations } = await openAuth();
  try {
    page.toggleAccountConsent();
    assert.equal(page.data.accountAgreed, true);
    assert.match(renderPage("pages/auth/index", page.data), /<button[^>]*>登录/);
    await page.onGetPhoneNumber({ detail: { code: "phone-1" } });
    assert.deepEqual(logins, [{ phoneCode: "phone-1" }]);
    assert.deepEqual(navigations().filter(([name]) => name === "switchTab"), [["switchTab", { url: "/pages/home/index" }]]);
    assert.equal(
      harness.wxCalls.filter((call) => call.name === "setStorageSync" && call.args[0] === CHILD_INFO_CONSENT_KEY).length,
      0,
    );
  } finally {
    harness.restore();
  }
});

test("returning to the login page does not check the box", async () => {
  const { harness, page } = await openAuth();
  try {
    page.onShow();
    assert.equal(page.data.accountAgreed, false);
    page.toggleAccountConsent();
    page.onShow();
    assert.equal(page.data.accountAgreed, true);
  } finally {
    harness.restore();
  }
});

test("a failed login stays on the page and does not keep a phone request in flight", async () => {
  const { harness, page, logins } = await openAuth({ loginError: "手机号已绑定其他账号" });
  try {
    page.toggleAccountConsent();
    await page.onGetPhoneNumber({ detail: { code: "phone-3" } });
    assert.equal(page.data.error, "手机号已绑定其他账号");
    assert.equal(page.data.loading, false);
    assert.deepEqual(logins, [{ phoneCode: "phone-3" }]);
  } finally {
    harness.restore();
  }
});
