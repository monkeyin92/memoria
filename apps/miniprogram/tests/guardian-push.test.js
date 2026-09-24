const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const guardianPush = require("../utils/guardian-push");

const root = path.resolve(__dirname, "..");

function fakeApi({ config, loginCode = "login-code-1" } = {}) {
  const calls = { config: 0, login: 0, recorded: [] };
  return {
    calls,
    async getGuardianPushConfig() {
      calls.config += 1;
      if (config instanceof Error) throw config;
      return config;
    },
    async wechatLoginCode() {
      calls.login += 1;
      return loginCode;
    },
    async recordGuardianPushSubscription(body) {
      calls.recorded.push(body);
      return { template_id: body.templateId, result: body.result, remaining: 1 };
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
  assert.match(config.explanation, /不含任何对话内容/);

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
  assert.match(template, /需要及时关注/);
  assert.match(script, /guardianPush\.subscribeCrisisAlerts/);
  assert.match(script, /guardianPush\.offerCrisisSubscription/);
  assert.match(api, /\/v1\/guardian\/push-config/);
  assert.match(api, /\/v1\/guardian\/push-subscriptions/);
});
