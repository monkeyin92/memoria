const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const crisisAlerts = require("../utils/crisis-alerts");
const guardianPush = require("../utils/guardian-push");

const root = path.resolve(__dirname, "..");

function fakeApi({ config, loginCode = "login-code-1" } = {}) {
  const calls = { recorded: [] };
  return {
    calls,
    async getGuardianPushConfig() {
      return config;
    },
    async getGuardianNotifications() {
      return { items: [] };
    },
    async wechatLoginCode() {
      return loginCode;
    },
    async recordGuardianPushSubscription(body) {
      calls.recorded.push(body);
      return { template_id: body.templateId, result: body.result, remaining: 1 };
    },
  };
}

function fakeWx(answer) {
  return {
    requestSubscribeMessage(options) {
      if (answer instanceof Error) options.fail(answer);
      else options.success({ errMsg: "requestSubscribeMessage:ok", ...answer });
    },
  };
}

test("normalizeAlert keeps the approved interrupted and victim lines and drops an empty detail", () => {
  const interrupted = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356";
  const victim = "如有危险请拨打110";
  const interruptedAlert = crisisAlerts.normalizeAlert({
    notification_id: "n9",
    pinned: true,
    message: "请尽快联系",
    detail: interrupted,
  });
  assert.equal(interruptedAlert.message, "有一条需要你关注的对话");
  assert.equal(interruptedAlert.detail, interrupted);

  const victimAlert = crisisAlerts.normalizeAlert({
    notification_id: "n-victim",
    pinned: true,
    message: "请尽快联系",
    detail: victim,
  });
  assert.equal(victimAlert.message, "有一条需要你关注的对话");
  assert.equal(victimAlert.detail, victim);

  const plain = crisisAlerts.normalizeAlert({
    notification_id: "n8",
    pinned: true,
    message: "有一条需要你关注的对话",
    detail: "",
  });
  assert.equal(Object.hasOwn(plain, "detail"), false);
});

test("pinnedAlerts keeps only server-marked undelivered crisis rows", () => {
  const items = crisisAlerts.pinnedAlerts([
    {
      notification_id: "n1",
      minor_user_id: "child-a",
      minor_display_name: "小明",
      occurred_at: "2026-10-14T00:00:00+00:00",
      delivery_status: "no_subscription",
      pinned: true,
      message: "请尽快联系",
    },
    {
      notification_id: "n2",
      minor_user_id: "child-a",
      minor_display_name: "小明",
      occurred_at: "2026-10-13T00:00:00+00:00",
      delivery_status: "delivered",
      pinned: false,
      message: "已送达",
    },
  ]);

  assert.deepEqual(items, [
    {
      notificationId: "n1",
      minorUserId: "child-a",
      minorDisplayName: "小明",
      occurredAt: "2026-10-14T00:00:00+00:00",
      deliveryStatus: "no_subscription",
      pinned: true,
      message: "有一条需要你关注的对话",
    },
  ]);
});

test("requireCrisisSubscribeBeforeBind blocks parent bind without accept", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  const paused = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: false,
  });
  assert.deepEqual(paused, { ok: true, kind: "paused" });

  const unavailable = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config: { enabled: false, templateId: "" },
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(unavailable.ok, false);
  assert.equal(unavailable.kind, "unavailable");

  const rejected = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "reject" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(rejected.ok, false);
  assert.equal(rejected.kind, "not_accepted");

  const api = fakeApi();
  const accepted = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config,
    api,
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(accepted.ok, true);
  assert.equal(accepted.kind, "accepted");
  assert.deepEqual(api.calls.recorded, [
    { templateId: "tmpl-crisis", result: "accept", loginCode: "login-code-1" },
  ]);

  const selfUse = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "self_use",
    config,
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "reject" }),
    subscriptionPushEnabled: true,
  });
  assert.deepEqual(selfUse, { ok: true, kind: "not_required" });
});

test("bind keeps subscribe gate code but pauses UI while flag is off; memory pins alerts", () => {
  const bindJs = fs.readFileSync(path.join(root, "pages/bind/index.js"), "utf8");
  const bindWxml = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const memoryWxml = fs.readFileSync(path.join(root, "pages/memory/index.wxml"), "utf8");
  const homeWxml = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const configJs = fs.readFileSync(path.join(root, "config.js"), "utf8");

  assert.match(configJs, /SUBSCRIPTION_PUSH_ENABLED\s*=\s*false/);
  assert.match(bindJs, /requireCrisisSubscribeBeforeBind/);
  assert.match(bindJs, /createDeviceBinding/);
  assert.match(bindJs, /SUBSCRIPTION_PUSH_ENABLED/);
  const submitAt = bindJs.indexOf("async submitBinding");
  const gateAt = bindJs.indexOf("requireCrisisSubscribeBeforeBind", submitAt);
  const quietAt = bindJs.indexOf("confirmUnusualQuietHours", submitAt);
  const createAt = bindJs.indexOf("createDeviceBinding", submitAt);
  assert.ok(gateAt > submitAt && createAt > gateAt);
  // WeChat subscribe must start before any other await in the submit tap stack.
  assert.ok(quietAt > gateAt && quietAt < createAt);
  assert.match(bindWxml, /\{\{submitLabel\}\}/);
  assert.match(bindWxml, /subscriptionPushEnabled && crisisSubscribeRequired/);
  assert.doesNotMatch(bindJs, /offerCrisisSubscription/);
  assert.doesNotMatch(bindWxml, /危机提醒/);
  assert.doesNotMatch(memoryWxml, /危机提醒/);
  assert.doesNotMatch(homeWxml, /危机提醒/);
  assert.match(memoryWxml, /pinnedCrisisAlerts/);
  assert.match(memoryWxml, /subscriptionPushEnabled && crisisPush\.enabled/);
  assert.match(homeWxml, /pinnedCrisisAlerts|unreadSafetyAlert/);
});

test("pinned home and memory alerts stay independent of the push toggle", () => {
  const homeJs = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  const memoryJs = fs.readFileSync(path.join(root, "pages/memory/index.js"), "utf8");
  const deviceJs = fs.readFileSync(path.join(root, "pages/device/index.js"), "utf8");
  assert.match(homeJs, /loadPinnedCrisisAlerts/);
  assert.match(memoryJs, /loadPinnedCrisisAlerts/);
  assert.match(homeJs, /safetySubscribeOn|optOutCrisisPushAlerts/);
  assert.doesNotMatch(memoryJs, /safetySubscribeOn|optOutCrisisPushAlerts/);
  // Opting out only clears the WeChat push ledger; it must not clear pinned rows.
  for (const script of [deviceJs, homeJs]) {
    const optOutAt = script.indexOf("optOutCrisisPushAlerts");
    assert.ok(optOutAt > 0);
    const optOutChunk = script.slice(optOutAt, optOutAt + 500);
    assert.doesNotMatch(optOutChunk, /pinnedCrisisAlerts|elderAlerts\s*:\s*\[\]/);
  }
  assert.equal(
    crisisAlerts.pinnedAlerts([
      {
        notification_id: "n-pin",
        pinned: true,
        delivery_status: "no_subscription",
        message: "有一条需要你关注的对话",
      },
    ]).length,
    1,
  );
});
