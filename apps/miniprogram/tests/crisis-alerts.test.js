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
      message: "请尽快联系",
    },
  ]);
});

test("requireCrisisSubscribeBeforeBind blocks parent bind without accept", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  const unavailable = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config: { enabled: false, templateId: "" },
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
  });
  assert.equal(unavailable.ok, false);
  assert.equal(unavailable.kind, "unavailable");

  const rejected = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "reject" }),
  });
  assert.equal(rejected.ok, false);
  assert.equal(rejected.kind, "not_accepted");

  const api = fakeApi();
  const accepted = await crisisAlerts.requireCrisisSubscribeBeforeBind({
    declaredMode: "parent_for_child",
    config,
    api,
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
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
  });
  assert.deepEqual(selfUse, { ok: true, kind: "not_required" });
});

test("bind requires subscribe before create; memory pins undelivered alerts", () => {
  const bindJs = fs.readFileSync(path.join(root, "pages/bind/index.js"), "utf8");
  const bindWxml = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const memoryWxml = fs.readFileSync(path.join(root, "pages/memory/index.wxml"), "utf8");
  const homeWxml = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");

  assert.match(bindJs, /requireCrisisSubscribeBeforeBind/);
  assert.match(bindJs, /createDeviceBinding/);
  const gateAt = bindJs.indexOf("requireCrisisSubscribeBeforeBind");
  const createAt = bindJs.indexOf("createDeviceBinding");
  assert.ok(gateAt > 0 && createAt > gateAt);
  assert.match(bindWxml, /开启提醒并绑定/);
  assert.match(bindWxml, /安全提醒是完成绑定的前提/);
  assert.doesNotMatch(bindJs, /offerCrisisSubscription/);
  assert.match(memoryWxml, /pinnedCrisisAlerts/);
  assert.match(homeWxml, /pinnedCrisisAlerts/);
});
