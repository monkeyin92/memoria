const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const guardianPush = require("../utils/guardian-push");

const root = path.resolve(__dirname, "..");

function fakeApi({ config, subscription, loginCode = "login-code-1" } = {}) {
  const calls = { config: 0, subscription: 0, login: 0, recorded: [] };
  return {
    calls,
    async getGuardianPushConfig() {
      calls.config += 1;
      if (config instanceof Error) throw config;
      return config;
    },
    async getGuardianPushSubscription() {
      calls.subscription += 1;
      if (subscription instanceof Error) throw subscription;
      return subscription;
    },
    async wechatLoginCode() {
      calls.login += 1;
      return loginCode;
    },
    async recordGuardianPushSubscription(body) {
      calls.recorded.push(body);
      const remaining = body.result === "accept" ? 1 : 0;
      return { template_id: body.templateId, result: body.result, remaining };
    },
  };
}

function fakeWx(answer) {
  const prompts = [];
  return {
    prompts,
    requestSubscribeMessage(options) {
      prompts.push(options.tmplIds);
      if (answer instanceof Error) options.fail(answer);
      else options.success({ errMsg: "requestSubscribeMessage:ok", ...answer });
    },
    showModal(options) {
      prompts.push("modal");
      options.success({ confirm: true });
    },
  };
}

test("disabled push config never prompts or records", async () => {
  for (const config of [
    { enabled: false, template_ids: {} },
    { enabled: true, template_ids: {} },
    { enabled: false, template_ids: { crisis: "tmpl" } },
    null,
    new Error("offline"),
  ]) {
    const api = fakeApi({ config });
    const wx = fakeWx({ tmpl: "accept" });
    const loaded = await guardianPush.loadCrisisPushConfig(api);
    assert.equal(loaded.enabled, false);
    assert.equal(loaded.templateId, "");
    const outcome = await guardianPush.subscribeCrisisAlerts({ config: loaded, api, wxApi: wx });
    const offered = await guardianPush.offerCrisisSubscription({ config: loaded, api, wxApi: wx });
    assert.deepEqual(outcome, { prompted: false, result: "" });
    assert.deepEqual(offered, { prompted: false, result: "" });
    assert.deepEqual(wx.prompts, []);
    assert.deepEqual(api.calls.recorded, []);
    assert.equal(api.calls.login, 0);
  }
});

test("enabled push prompts for the crisis template and posts an accepted result", async () => {
  const api = fakeApi({ config: { enabled: true, template_ids: { crisis: " tmpl-crisis " } } });
  const wx = fakeWx({ "tmpl-crisis": "accept" });

  const config = await guardianPush.loadCrisisPushConfig(api);
  assert.equal(config.enabled, true);
  assert.equal(config.templateId, "tmpl-crisis");
  assert.match(config.explanation, /订阅安全提醒/);
  assert.match(config.explanation, /不含任何对话内容/);
  assert.equal(guardianPush.PARENT_SUBSCRIBE_LABEL, "订阅安全提醒");

  const outcome = await guardianPush.subscribeCrisisAlerts({ config, api, wxApi: wx });

  assert.deepEqual(outcome, { prompted: true, result: "accept", recorded: true });
  assert.deepEqual(wx.prompts, [["tmpl-crisis"]]);
  assert.equal(api.calls.login, 1);
  assert.deepEqual(api.calls.recorded, [
    { templateId: "tmpl-crisis", result: "accept", loginCode: "login-code-1" },
  ]);
});

test("reject, ban and main-switch-off are recorded without a login code", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  for (const [answer, expected] of [
    [{ "tmpl-crisis": "reject" }, "reject"],
    [{ "tmpl-crisis": "ban" }, "ban"],
    [Object.assign(new Error("main switch off"), { errCode: 20004 }), "ban"],
  ]) {
    const api = fakeApi();
    const outcome = await guardianPush.subscribeCrisisAlerts({
      config,
      api,
      wxApi: fakeWx(answer),
    });
    assert.equal(outcome.result, expected);
    assert.equal(api.calls.login, 0);
    assert.deepEqual(api.calls.recorded, [
      { templateId: "tmpl-crisis", result: expected, loginCode: "" },
    ]);
  }

  const filtered = fakeApi();
  const outcome = await guardianPush.subscribeCrisisAlerts({
    config,
    api: filtered,
    wxApi: fakeWx({ "tmpl-crisis": "filter" }),
  });
  assert.deepEqual(outcome, { prompted: true, result: "filter", recorded: false });
  assert.deepEqual(filtered.calls.recorded, []);
});

test("the post-binding offer explains first and subscribes only on confirm", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  const api = fakeApi();
  const wx = fakeWx({ "tmpl-crisis": "accept" });
  const accepted = await guardianPush.offerCrisisSubscription({ config, api, wxApi: wx });
  assert.equal(accepted.result, "accept");
  assert.deepEqual(wx.prompts, ["modal", ["tmpl-crisis"]]);

  const declined = fakeApi();
  const declineWx = {
    prompts: [],
    showModal(options) {
      options.success({ confirm: false, cancel: true });
    },
    requestSubscribeMessage() {
      throw new Error("must not prompt after cancel");
    },
  };
  assert.deepEqual(
    await guardianPush.offerCrisisSubscription({ config, api: declined, wxApi: declineWx }),
    { prompted: false, result: "" },
  );
  assert.deepEqual(declined.calls.recorded, []);
});

test("guardian page wires the subscribe prompt to a tap and keeps alerts visible", () => {
  const script = fs.readFileSync(path.join(root, "pages/guardian/index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages/guardian/index.wxml"), "utf8");
  const api = fs.readFileSync(path.join(root, "utils/api.js"), "utf8");

  assert.match(template, /wx:if="\{\{crisisPush\.enabled\}\}"/);
  assert.match(template, /bindtap="enableCrisisPush"/);
  assert.match(template, /订阅安全提醒/);
  assert.doesNotMatch(template, /危机提醒/);
  assert.match(template, /需要及时关注/);
  assert.match(script, /guardianPush\.subscribeCrisisAlerts/);
  assert.match(script, /guardianPush\.offerCrisisSubscription/);
  assert.match(api, /\/v1\/guardian\/push-config/);
  assert.match(api, /\/v1\/guardian\/push-subscriptions/);
  assert.match(api, /getGuardianPushSubscription/);
});

test("settings toggle loads remaining > 0 as on and opt_out clears the ledger", async () => {
  const enabledConfig = { enabled: true, template_ids: { crisis: "tmpl-crisis" } };
  const onApi = fakeApi({
    config: enabledConfig,
    subscription: { template_id: "tmpl-crisis", remaining: 2, subscribed: true, last_result: "accept" },
  });
  const onState = await guardianPush.loadSafetySubscribeState(onApi);
  assert.equal(onState.safetySubscribeKnown, true);
  assert.equal(onState.safetySubscribeOn, true);
  assert.equal(onState.subscription.remaining, 2);
  assert.equal(onApi.calls.config, 1);
  assert.equal(onApi.calls.subscription, 1);

  const offApi = fakeApi({
    config: enabledConfig,
    subscription: { template_id: "tmpl-crisis", remaining: 0, subscribed: false, last_result: "opt_out" },
  });
  const offState = await guardianPush.loadSafetySubscribeState(offApi);
  assert.equal(offState.safetySubscribeKnown, true);
  assert.equal(offState.safetySubscribeOn, false);
  assert.deepEqual(
    guardianPush.safetySubscribeView(offState),
    {
      safetySubscribeKnown: true,
      safetySubscribeOn: false,
      safetySubscribeOffCaption: guardianPush.SAFETY_SUBSCRIBE_OFF_CAPTION,
    },
  );

  const config = guardianPush.normalizeCrisisPushConfig(enabledConfig);
  const cleared = await guardianPush.optOutCrisisPushAlerts({ config, api: offApi });
  assert.deepEqual(cleared, { ok: true, remaining: 0 });
  assert.deepEqual(offApi.calls.recorded, [
    { templateId: "tmpl-crisis", result: "opt_out", loginCode: "" },
  ]);
  assert.equal(guardianPush.SAFETY_SUBSCRIBE_OFF_TITLE, "关闭安全提醒推送？");
  assert.equal(
    guardianPush.SAFETY_SUBSCRIBE_OFF_CONFIRM,
    "关闭后，我们不再通过微信给你推送安全提醒。如果孩子聊到需要你关注的话题，只会在你下次打开小程序时置顶显示，你可能不能及时知道。",
  );
  assert.equal(guardianPush.SAFETY_SUBSCRIBE_OFF_KEEP, "保持开启");
  assert.equal(guardianPush.SAFETY_SUBSCRIBE_OFF_PROCEED, "仍要关闭");
  assert.equal(
    guardianPush.SAFETY_SUBSCRIBE_OFF_CAPTION,
    "安全提醒推送已关闭，打开小程序仍会看到置顶提醒。",
  );
  assert.doesNotMatch(guardianPush.SAFETY_SUBSCRIBE_OFF_CONFIRM, /无法再推送|永久关闭|危机/);
  assert.doesNotMatch(guardianPush.SAFETY_SUBSCRIBE_OFF_CAPTION, /无法再推送|永久关闭|危机/);
});

test("subscription-state query failure is unknown, not pretended off", async () => {
  const api = fakeApi({
    config: { enabled: true, template_ids: { crisis: "tmpl-crisis" } },
    subscription: new Error("network down"),
  });
  const state = await guardianPush.loadSafetySubscribeState(api);
  assert.equal(state.safetySubscribeKnown, false);
  assert.equal(state.safetySubscribeOn, false);
  assert.equal(state.subscription.known, false);
  assert.deepEqual(guardianPush.safetySubscribeView(state), {
    safetySubscribeKnown: false,
    safetySubscribeOn: false,
    safetySubscribeOffCaption: guardianPush.SAFETY_SUBSCRIBE_UNKNOWN_CAPTION,
  });
  assert.equal(guardianPush.SAFETY_SUBSCRIBE_UNKNOWN_CAPTION, "状态获取失败，点击重试");
  assert.notEqual(
    guardianPush.safetySubscribeView(state).safetySubscribeOffCaption,
    guardianPush.SAFETY_SUBSCRIBE_OFF_CAPTION,
  );
});

test("device settings toggle uses ledger state and real opt-out, not UI-only off", () => {
  const script = fs.readFileSync(path.join(root, "pages/device/index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages/device/index.wxml"), "utf8");
  assert.match(template, /安全提醒订阅/);
  assert.match(template, /safetySubscribeOffCaption/);
  assert.match(template, /safetySubscribeKnown && safetySubscribeOn/);
  assert.match(template, /disabled="\{\{!safetySubscribeKnown \|\| safetySubscribeBusy\}\}"/);
  assert.match(template, /bindtap="retrySafetySubscribeState"/);
  assert.match(template, /bindchange="onSafetySubscribeChange"/);
  assert.match(script, /loadSafetySubscribeState/);
  assert.match(script, /safetySubscribeView/);
  assert.match(script, /retrySafetySubscribeState/);
  assert.match(script, /optOutCrisisPushAlerts/);
  assert.match(script, /SAFETY_SUBSCRIBE_OFF_TITLE/);
  assert.match(script, /SAFETY_SUBSCRIBE_OFF_KEEP/);
  assert.match(script, /SAFETY_SUBSCRIBE_OFF_PROCEED/);
  assert.match(script, /subscribeCrisisAlerts/);
  assert.doesNotMatch(script, /无法再推送|永久关闭/);
  assert.doesNotMatch(script, /危机/);
});
