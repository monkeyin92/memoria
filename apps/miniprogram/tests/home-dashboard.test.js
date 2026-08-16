const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const homePath = require.resolve("../pages/home/index");
const apiPath = require.resolve("../utils/api");
const bindingPath = require.resolve("../utils/device-binding");
const contractsPath = require.resolve("../utils/multi-subject-contracts");

function today() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function setup({ binding = null, authenticated = true } = {}) {
  const previousPage = global.Page;
  const previousGetApp = global.getApp;
  const previousWx = global.wx;
  let page;
  let profileCalls = 0;
  let runtimeCalls = 0;
  let activationCalls = 0;
  let memoryCalls = 0;
  const settingsUpdates = [];

  const api = require(apiPath);
  api.hasAuthenticatedSession = () => authenticated;
  api.currentIdentity = () => ({ user_id: "person_owner" });
  api.currentAuthEpoch = () => 0;
  api.isAuthEpochCurrent = () => true;
  api.getProfile = async () => {
    profileCalls += 1;
    return { display_name: "小明", companion_id: "taoxi", subject_category: "minor" };
  };
  api.getRuntimeProfile = async () => {
    runtimeCalls += 1;
    return {
      valid: true,
      active_subject_id: "person_owner",
      persona: { persona_id: "persona-1", version: 3, relationship_stage: "熟悉" },
    };
  };
  api.getActivationStatus = async () => {
    activationCalls += 1;
    return {
      status: "ready_for_conversation",
      firmware_version: "0.9.1",
      network: { internet: true, status: "wifi" },
    };
  };
  api.getDeviceSettings = async () => ({
    device_id: "dev_dashboard",
    settings_version: 3,
    volume_limit: 72,
    screen_brightness: 80,
    night_mode: false,
    do_not_disturb: false,
    learning_mode: "off",
    audio_mode: "half_duplex_safe",
    wake_mode: "button",
    allowed_barge_in: ["button"],
  });
  api.updateDeviceSettings = async (deviceId, changes, options) => {
    settingsUpdates.push({ deviceId, changes, options });
    return {
      ...(await api.getDeviceSettings()),
      ...changes,
      settings_version: options.expectedVersion + 1,
    };
  };
  api.requireRuntimeCapability = async () => ({ allowed: true, reason: "allowed" });
  api.getMemoryDays = async () => {
    memoryCalls += 1;
    return {
      items: [{ day: today(), message_count: 5, summary: { overview: "今天聊了学校的事" } }],
    };
  };
  api.getGuardianNotifications = async () => ({ items: [{ id: "notice-1" }] });

  require.cache[bindingPath] = {
    exports: { readBindingManifest: () => binding },
  };
  require.cache[contractsPath] = {
    exports: { Capability: { MemoryRecallPrivate: "memory_recall_private" } },
  };

  global.Page = (definition) => {
    page = definition;
  };
  global.getApp = () => ({
    subscribeAuthCleared: () => () => {},
  });
  global.wx = {
    switchTab() {},
    navigateTo() {},
    stopPullDownRefresh() {},
  };
  delete require.cache[homePath];
  require(homePath);

  const instance = {
    ...page,
    data: JSON.parse(JSON.stringify(page.data)),
    setData(update) {
      Object.assign(this.data, update);
    },
  };
  return {
    page,
    instance,
    counts: { profileCalls: () => profileCalls, runtimeCalls: () => runtimeCalls, activationCalls: () => activationCalls, memoryCalls: () => memoryCalls },
    settingsUpdates,
    cleanup() {
      delete require.cache[homePath];
      delete require.cache[bindingPath];
      delete require.cache[contractsPath];
      if (previousPage === undefined) delete global.Page;
      else global.Page = previousPage;
      if (previousGetApp === undefined) delete global.getApp;
      else global.getApp = previousGetApp;
      if (previousWx === undefined) delete global.wx;
      else global.wx = previousWx;
    },
  };
}

test("home is a dashboard with no realtime voice surface", () => {
  const script = fs.readFileSync(path.join(root, "pages", "home", "index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages", "home", "index.wxml"), "utf8");
  assert.doesNotMatch(script, /MiniProgramMediaSession|media-gateway|media-protocol|pcm-player|transcript-events/);
  assert.doesNotMatch(script, /startVoice|startText|_connectMedia|scope\.record|connectSocket|getRecorderManager/);
  assert.doesNotMatch(template, /开始语音对话|使用文字对话|实时对话|恢复对话|结束语音对话/);
  assert.match(template, /机器人状态/);
  assert.match(template, /今日概览/);
  assert.match(template, /伙伴与使用者/);
  assert.match(template, /快捷设置/);
});

test("bound dashboard renders server-authoritative device, persona and overview data", async () => {
  const env = setup({
    binding: { device_id: "dev_dashboard", binding_id: "bd_dashboard", binding_version: 1 },
  });
  try {
    await env.page.loadDashboard.call(env.instance);
    assert.equal(env.instance.data.hasBinding, true);
    assert.equal(env.instance.data.device.online, true);
    assert.equal(env.instance.data.device.onlineLabel, "在线，可直接对话");
    assert.equal(env.instance.data.device.firmwareVersion, "0.9.1");
    assert.equal(env.instance.data.persona.relationship_stage, "熟悉");
    assert.equal(env.instance.data.persona.version, 3);
    assert.equal(env.instance.data.activeSubjectLabel, "已确认使用者");
    assert.equal(env.instance.data.todayCount, 5);
    assert.equal(env.instance.data.recentSummary, "今天聊了学校的事");
    assert.equal(env.instance.data.guardianNoticeCount, 1);
    assert.equal(env.instance.data.settings.settings_version, 3);
    assert.equal(env.counts.runtimeCalls(), 1);
    assert.equal(env.counts.activationCalls(), 1);
    assert.equal(env.counts.memoryCalls(), 1);
  } finally {
    env.cleanup();
  }
});

test("quick settings persist through HTTPS and accept only server-confirmed state", async () => {
  const env = setup({
    binding: { device_id: "dev_dashboard", binding_id: "bd_dashboard", binding_version: 1 },
  });
  try {
    await env.page.loadDashboard.call(env.instance);
    await env.page.selectLearningMode.call(env.instance, {
      currentTarget: { dataset: { mode: "tutor_english" } },
    });
    assert.deepEqual(env.settingsUpdates[0], {
      deviceId: "dev_dashboard",
      changes: { learning_mode: "tutor_english" },
      options: { expectedVersion: 3 },
    });
    assert.equal(env.instance.data.settings.learning_mode, "tutor_english");
    assert.equal(env.instance.data.settings.settings_version, 4);
  } finally {
    env.cleanup();
  }
});

test("unbound dashboard shows onboarding entry and touches no device media state", async () => {
  const env = setup({ binding: null });
  try {
    await env.page.loadDashboard.call(env.instance);
    assert.equal(env.instance.data.hasBinding, false);
    assert.equal(env.instance.data.device, null);
    assert.equal(env.instance.data.activeSubjectLabel, "");
    assert.equal(env.counts.runtimeCalls(), 0);
    assert.equal(env.counts.activationCalls(), 0);
    const template = fs.readFileSync(path.join(root, "pages", "home", "index.wxml"), "utf8");
    assert.match(template, /添加机器人/);
  } finally {
    env.cleanup();
  }
});

test("guest home keeps browsing without profile or memory calls", () => {
  const env = setup({ binding: null, authenticated: false });
  try {
    env.page.onLoad.call(env.instance);
    env.page.onShow.call(env.instance);
    assert.equal(env.instance.data.authenticated, false);
    assert.equal(env.instance.data.hasBinding, false);
    assert.equal(env.instance.data.device, null);
    assert.equal(env.instance.data.todayCount, null);
    assert.equal(env.counts.profileCalls(), 0);
    assert.equal(env.counts.memoryCalls(), 0);
  } finally {
    env.cleanup();
  }
});
