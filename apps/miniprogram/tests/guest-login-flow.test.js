const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");

function read(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("all three tab pages keep a useful guest state instead of redirecting on show", () => {
  const home = read("pages/home/index.js");
  const memory = read("pages/memory/index.wxml");
  const profile = read("pages/profile/index.wxml");

  assert.doesNotMatch(
    home,
    /onShow\(\)\s*\{[\s\S]{0,220}navigateTo\(\{\s*url:\s*"\/pages\/auth\/index"/,
  );
  assert.match(memory, /wx:if="\{\{!authenticated\}\}"[\s\S]*登录后查看你的专属回顾/);
  assert.match(profile, /wx:if="\{\{!authenticated\}\}"[\s\S]*游客浏览模式/);
});

test("guest actions share one login gate with a return path", async () => {
  const apiPath = require.resolve("../utils/api");
  const gatePath = require.resolve("../utils/auth-gate");
  const previousWx = global.wx;
  const previousGetCurrentPages = global.getCurrentPages;
  let navigated = "";

  require.cache[apiPath] = {
    exports: {
      hasAuthenticatedSession: () => false,
      currentIdentity: () => null,
      restoreWechatIdentity: async () => {
        throw Object.assign(new Error("需要手机号授权"), {
          code: "phone_authorization_required",
        });
      },
    },
  };
  global.wx = {
    navigateTo({ url }) {
      navigated = url;
    },
  };
  global.getCurrentPages = () => [{ route: "pages/home/index", options: {} }];
  delete require.cache[gatePath];
  const { requireLogin } = require("../utils/auth-gate");

  try {
    assert.equal(await requireLogin({ reason: "view_dashboard" }), false);
    assert.match(navigated, /^\/pages\/auth\/index\?/);
    assert.match(decodeURIComponent(navigated), /redirect=\/pages\/home\/index/);
    assert.match(navigated, /reason=view_dashboard/);
    assert.match(navigated, /skip_restore=1/);
  } finally {
    delete require.cache[gatePath];
    delete require.cache[apiPath];
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetCurrentPages === undefined) delete global.getCurrentPages;
    else global.getCurrentPages = previousGetCurrentPages;
  }
});

test("login uses WeChat phone authorization with optional nickname and avatar completion", () => {
  const authTemplate = read("pages/auth/index.wxml");
  const authScript = read("pages/auth/index.js");

  assert.match(authTemplate, /open-type="getPhoneNumber"/);
  assert.match(authTemplate, /open-type="chooseAvatar"/);
  assert.match(authTemplate, /type="nickname"/);
  assert.match(authTemplate, /《隐私保护指引》/);
  assert.doesNotMatch(authTemplate, />用户名</);
  assert.doesNotMatch(authTemplate, />密码</);
  assert.doesNotMatch(authTemplate, /开始匿名体验/);
  assert.match(authScript, /loginWithWechat/);
  assert.match(authScript, /uploadWechatAvatar/);
  assert.match(authScript, /restoreWechatIdentity/);
  assert.match(authScript, /openPrivacyContract/);
  assert.match(authScript, /hasAuthenticatedSession\(\)[\s\S]*finishLogin\(\)/);
});

test("logout clears private profile state without relying on a tab navigation refresh", () => {
  const profileScript = read("pages/profile/index.js");

  assert.match(
    profileScript,
    /async _finishLogout\(\)[\s\S]{0,120}api\.logoutLocal\(\);[\s\S]{0,80}this\._enterGuestState\(\)/,
  );
  assert.match(profileScript, /_enterGuestState\(\)[\s\S]*authenticated:\s*false[\s\S]*identity:\s*null/);
  assert.doesNotMatch(
    profileScript,
    /async _finishLogout\(\)[\s\S]{0,240}wx\.switchTab/,
  );
});

test("auth cleanup broadcasts to every page that can hold private state", () => {
  const appScript = read("app.js");
  const homeScript = read("pages/home/index.js");
  const memoryScript = read("pages/memory/index.js");
  const profileScript = read("pages/profile/index.js");
  const privacyScript = read("pages/privacy/index.js");
  const digitalSelfScript = read("pages/digital-self/index.js");

  assert.match(appScript, /subscribeAuthCleared\(listener\)/);
  assert.match(appScript, /for \(const listener of \[\.\.\.this\._authClearedListeners\]\)/);
  assert.match(homeScript, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
  assert.match(
    homeScript,
    /_enterGuestState\(\)[\s\S]*hasBinding:\s*false[\s\S]*device:\s*null[\s\S]*todayCount:\s*null/,
  );
  assert.match(memoryScript, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
  assert.match(memoryScript, /_enterGuestState\(\)[\s\S]*days:\s*\[\]/);
  assert.match(profileScript, /subscribeAuthCleared\(\(\) => this\._enterGuestState\(\)\)/);
  assert.match(profileScript, /_enterGuestState\(\)[\s\S]*identity:\s*null[\s\S]*stats:/);
  assert.match(privacyScript, /subscribeAuthCleared\(\(\) => this\._clearPrivateState\(\)\)/);
  assert.match(privacyScript, /_clearPrivateState\(\)[\s\S]*consent:\s*null/);
  assert.match(
    digitalSelfScript,
    /subscribeAuthCleared\(\(\) => this\._clearPrivateState\(\)\)/,
  );
  assert.match(
    digitalSelfScript,
    /_clearPrivateState\(\)[\s\S]*persona:\s*null[\s\S]*versions:\s*\[\]/,
  );
});

test("an authenticated 401 clears the persisted identity before returning the error", async () => {
  const apiPath = require.resolve("../utils/api");
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  let cleared = 0;

  global.getApp = () => ({
    globalData: {
      identity: { user_id: "wx_test", account_type: "registered" },
      accessToken: "access-token",
      accessTokenExpiresAt: Date.now() + 60_000,
    },
    clearAuthenticatedIdentity() {
      cleared += 1;
    },
  });
  global.wx = {
    request({ success }) {
      success({ statusCode: 401, data: { detail: "invalid access session" } });
    },
  };
  delete require.cache[apiPath];
  const api = require("../utils/api");

  try {
    await assert.rejects(api.getGrowthOverview(), (error) => error.status === 401);
    assert.equal(cleared, 1);
  } finally {
    delete require.cache[apiPath];
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
  }
});

test("a late review response cannot repopulate private data after auth is cleared", async () => {
  const appPath = require.resolve("../app");
  const apiPath = require.resolve("../utils/api");
  const gatePath = require.resolve("../utils/auth-gate");
  const memoryPath = require.resolve("../pages/memory/index");
  const previousApp = global.App;
  const previousPage = global.Page;
  const previousGetApp = global.getApp;
  const previousWx = global.wx;
  let appDefinition;
  let memoryPage;
  let releaseDays;
  const daysReady = new Promise((resolve) => {
    releaseDays = resolve;
  });

  global.wx = {
    getStorageSync() {
      return null;
    },
    setStorageSync() {},
    removeStorageSync() {},
  };
  global.App = (definition) => {
    appDefinition = definition;
  };
  delete require.cache[appPath];
  require("../app");
  const app = {
    ...appDefinition,
    globalData: { ...appDefinition.globalData },
    _authClearedListeners: new Set(),
  };
  app.setAuthenticatedIdentity({
    user_id: "wx_private",
    account_type: "registered",
    access_token: "access-token",
    expires_in: 60,
  });
  global.getApp = () => app;

  delete require.cache[apiPath];
  delete require.cache[gatePath];
  const api = require("../utils/api");
  api.getMemoryDays = async () => daysReady;
  global.Page = (definition) => {
    memoryPage = definition;
  };
  delete require.cache[memoryPath];
  require("../pages/memory/index");
  const instance = {
    data: { ...memoryPage.data },
    _enterGuestState: memoryPage._enterGuestState,
    setData(update) {
      Object.assign(this.data, update);
    },
  };

  try {
    memoryPage.onLoad.call(instance);
    const pending = memoryPage.loadDays.call(instance);
    await new Promise((resolve) => setImmediate(resolve));
    app.clearAuthenticatedIdentity();
    releaseDays({
      items: [
        {
          day: "2026-07-28",
          summary: { overview: "不应重新出现在游客页面的私人回顾" },
        },
      ],
    });
    await pending;

    assert.equal(app.globalData.authEpoch, 2);
    assert.equal(instance.data.authenticated, false);
    assert.equal(instance.data.loading, false);
    assert.deepEqual(instance.data.days, []);
  } finally {
    memoryPage?.onUnload?.call(instance);
    delete require.cache[memoryPath];
    delete require.cache[gatePath];
    delete require.cache[apiPath];
    delete require.cache[appPath];
    if (previousApp === undefined) delete global.App;
    else global.App = previousApp;
    if (previousPage === undefined) delete global.Page;
    else global.Page = previousPage;
    if (previousGetApp === undefined) delete global.getApp;
    else global.getApp = previousGetApp;
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
  }
});
