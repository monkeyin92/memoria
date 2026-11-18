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

async function withWxRequest(respond, fn) {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const requests = [];
  global.wx = {
    getStorageSync: () => undefined,
    setStorageSync() {},
    removeStorageSync() {},
    request(options) {
      requests.push({ method: options.method || "GET", url: options.url });
      respond(options);
    },
  };
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "u_parent", display_name: "乐乐妈妈" },
      accessToken: "test-token",
      accessTokenExpiresAt: Date.now() + 3600_000,
      authEpoch: 0,
    },
  });
  try {
    return await fn(requests);
  } finally {
    global.wx = previousWx;
    global.getApp = previousGetApp;
  }
}

test("parseSafetyAlert keeps a server detail sentence and drops an empty one", () => {
  const selfHarm = "这次的提醒话术孩子没有听完，如需帮助可拨打心理援助热线12356";
  const victim = "如有危险请拨打110";
  const selfHarmAlert = crisisAlerts.parseSafetyAlert({
    notification_id: "n9",
    message: "请尽快联系",
    summary: "孩子说到了和自己情绪、安全有关的内容",
    detail: selfHarm,
  });
  assert.equal(selfHarmAlert.message, "有一条需要你关注的对话");
  assert.equal(selfHarmAlert.detail, selfHarm);
  assert.equal(selfHarmAlert.summary.includes("不想活"), false);

  const victimAlert = crisisAlerts.parseSafetyAlert({
    notification_id: "n-victim",
    message: "请尽快联系",
    summary: "孩子说到了自己可能受到伤害",
    detail: victim,
  });
  assert.equal(victimAlert.message, "有一条需要你关注的对话");
  assert.equal(victimAlert.detail, victim);
  assert.equal(victimAlert.detail.includes("如有危险请拨打110"), true);
  assert.equal(victimAlert.summary.includes("打我"), false);

  const plain = crisisAlerts.parseSafetyAlert({
    notification_id: "n8",
    message: "有一条需要你关注的对话",
    detail: "",
  });
  assert.equal(Object.hasOwn(plain, "detail"), false);

  const relay = crisisAlerts.parseSafetyAlert({
    notification_id: "n7",
    summary: "孩子提到了别人的安全，或在问怎么预防",
  });
  assert.equal(Object.hasOwn(relay, "detail"), false);
});

test("parseSafetyAlert drops an item only when its id is missing", () => {
  assert.equal(crisisAlerts.parseSafetyAlert(null), null);
  assert.equal(crisisAlerts.parseSafetyAlert({ category_label: "情绪和安全" }), null);
  assert.deepEqual(
    crisisAlerts.parseSafetyAlert({ notification_id: "n1", delivery_status: "delivered", message: "已送达", summary: 7 }),
    {
      notificationId: "n1",
      minorUserId: "",
      minorDisplayName: "TA",
      occurredAt: "",
      occurredLabel: "",
      message: "有一条需要你关注的对话",
      categoryLabel: "",
      summary: "",
      robotSaid: "",
      parentAction: "",
    },
  );
});

test("parseSafetyAlert keeps delivery_receipt and maps #543 stage labels", () => {
  const cases = [
    ["recorded", "recorded", "已记录"],
    ["queued", "queued", "排队中"],
    ["queued", "pending_retry", "重试"],
    ["queued", "pin_only", "排队中"],
    ["channel_accepted", "channel_accepted", "渠道已接受"],
    ["fail_or_auth_insufficient", "failed", "发送失败"],
    ["fail_or_auth_insufficient", "no_subscription", "无订阅"],
    ["fail_or_auth_insufficient", "guardian_not_eligible", "发送失败"],
    ["guardian_confirmed", "guardian_confirmed", "已查看"],
  ];
  for (const [stage, result, label] of cases) {
    const alert = crisisAlerts.parseSafetyAlert({
      notification_id: `n-${stage}-${result}`,
      delivery_receipt: {
        current_stage: stage,
        channel_accepted: stage === "channel_accepted" || stage === "guardian_confirmed",
        guardian_confirmed: stage === "guardian_confirmed",
        attempts: result === "pending_retry" ? 1 : 0,
        last_error_code: null,
        stages: [{ stage, event_id: "e1", at: "2026-11-04T06:00:00+00:00", result, attempt: null, reason: null }],
      },
    });
    assert.equal(alert.stageLabel, label, `${stage}/${result}`);
    assert.equal(alert.deliveryReceipt.currentStage, stage);
    assert.equal(alert.deliveryReceipt.stageLabel, label);
  }

  const missing = crisisAlerts.parseSafetyAlert({ notification_id: "n-bare" });
  assert.equal(Object.hasOwn(missing, "deliveryReceipt"), false);
  assert.equal(Object.hasOwn(missing, "stageLabel"), false);

  assert.equal(
    crisisAlerts.deliveryStageLabel({
      current_stage: "fail_or_auth_insufficient",
      stages: [{ stage: "fail_or_auth_insufficient", result: "no_subscription" }],
    }),
    "无订阅",
  );
});

test("loadUnreadSafetyAlerts distinguishes empty reads from auth, network, and server failures", async () => {
  const load = (getUnreadGuardianNotifications) => crisisAlerts.loadUnreadSafetyAlerts({ getUnreadGuardianNotifications });
  assert.deepEqual(await load(async () => ({ newest: null, others_count: 0 })), { status: "not_found" });
  assert.deepEqual(
    await load(async () => {
      throw Object.assign(new Error("expired"), { status: 401 });
    }),
    { status: "failed", errorKind: "auth" },
  );
  assert.deepEqual(
    await load(async () => {
      throw new Error("offline");
    }),
    { status: "failed", errorKind: "network" },
  );
  assert.deepEqual(
    await load(async () => {
      throw Object.assign(new Error("server"), { status: 503 });
    }),
    { status: "failed", errorKind: "server" },
  );
  const unread = await load(async () => ({ newest: { notification_id: "n3", category_label: "人身安全" }, others_count: 2 }));
  assert.equal(unread.status, "ok");
  assert.equal(unread.newest.notificationId, "n3");
  assert.equal(unread.newest.categoryLabel, "人身安全");
  assert.equal(unread.othersCount, 2);
  assert.equal((await load(async () => ({ newest: { notification_id: "n3" }, others_count: "2" }))).othersCount, 0);
});

test("loadSafetyAlert preserves a real 404 as not_found and distinguishes other failures", async () => {
  const asked = [];
  const api = {
    async getGuardianNotification(id) {
      asked.push(id);
      if (id !== "n1") throw Object.assign(new Error("guardian_notification_not_found"), { status: 404 });
      return { notification_id: "n1", summary: "孩子说到了自己可能受到伤害" };
    },
  };
  assert.deepEqual(await crisisAlerts.loadSafetyAlert(api, ""), { status: "not_found" });
  assert.deepEqual(await crisisAlerts.loadSafetyAlert(api, "n9"), { status: "not_found" });
  const loaded = await crisisAlerts.loadSafetyAlert(api, "n1");
  assert.equal(loaded.status, "ok");
  assert.equal(loaded.alert.summary, "孩子说到了自己可能受到伤害");
  assert.deepEqual(asked, ["n9", "n1"]);

  assert.deepEqual(
    await crisisAlerts.loadSafetyAlert(
      { getGuardianNotification: async () => { throw Object.assign(new Error("expired"), { status: 401 }); } },
      "n1",
    ),
    { status: "failed", errorKind: "auth" },
  );
  assert.deepEqual(
    await crisisAlerts.loadSafetyAlert(
      { getGuardianNotification: async () => { throw new Error("request:fail timeout"); } },
      "n1",
    ),
    { status: "failed", errorKind: "network" },
  );
});

test("acknowledgeSafetyAlert PUTs the receipt and says saved, gone, or failed", async () => {
  const api = require("../utils/api");
  const outcomes = [
    [(options) => options.success({ statusCode: 200, data: { notification_id: "n 1", read_at: "2026-10-16T11:42:00+00:00" } }), "saved"],
    [(options) => options.success({ statusCode: 404, data: { detail: { code: "guardian_notification_not_found" } } }), "gone"],
    [(options) => options.success({ statusCode: 503, data: {} }), "failed"],
    [(options) => options.fail({ errMsg: "request:fail timeout" }), "failed"],
  ];
  for (const [respond, expected] of outcomes) {
    await withWxRequest(respond, async (requests) => {
      assert.equal(await crisisAlerts.acknowledgeSafetyAlert(api, "n 1"), expected);
      assert.deepEqual(
        requests.map(({ method, url }) => [method, url.replace(/^.*(\/v1\/)/, "$1")]),
        [["PUT", "/v1/guardian/notifications/n%201/read"]],
      );
    });
  }
});

test("moreUnreadLabel counts the alerts not shown and says nothing for none", () => {
  assert.equal(crisisAlerts.moreUnreadLabel(0), "");
  assert.equal(crisisAlerts.moreUnreadLabel(undefined), "");
  assert.equal(crisisAlerts.moreUnreadLabel(1), "还有 1 条");
  assert.equal(crisisAlerts.moreUnreadLabel(2), "还有 2 条");
});

test("offerCrisisSubscribeAtBind never blocks: a rejected prompt, a missing template, a missing WeChat API and a failed record all let the bind go on", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  const rejected = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "reject" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(rejected.kind, "declined");
  assert.equal(typeof rejected.outcome, "object");
  assert.ok(!("ok" in rejected));

  const missing = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config: { enabled: false, templateId: "" },
    api: fakeApi(),
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(missing.kind, "unavailable");
  assert.ok(!("ok" in missing));

  const noWxApi = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: {},
    subscriptionPushEnabled: true,
  });
  assert.equal(noWxApi.kind, "unavailable");
  assert.ok(!("ok" in noWxApi));

  const failingApi = fakeApi();
  failingApi.recordGuardianPushSubscription = async () => {
    throw new Error("record failed");
  };
  const failed = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config,
    api: failingApi,
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(failed.kind, "failed");
  assert.ok(!("ok" in failed));
});

test("offerCrisisSubscribeAtBind records an accepted answer once", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  const api = fakeApi();
  const accepted = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config,
    api,
    wxApi: fakeWx({ "tmpl-crisis": "accept" }),
    subscriptionPushEnabled: true,
  });
  assert.equal(accepted.kind, "accepted");
  assert.deepEqual(api.calls.recorded, [
    { templateId: "tmpl-crisis", result: "accept", loginCode: "login-code-1" },
  ]);
});

test("offerCrisisSubscribeAtBind does not prompt for self_use or while paused", async () => {
  const config = guardianPush.normalizeCrisisPushConfig({
    enabled: true,
    template_ids: { crisis: "tmpl-crisis" },
  });
  let prompts = 0;
  const countingWx = {
    requestSubscribeMessage(options) {
      prompts += 1;
      options.success({ errMsg: "requestSubscribeMessage:ok", "tmpl-crisis": "accept" });
    },
  };
  const selfUse = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "self_use",
    config,
    api: fakeApi(),
    wxApi: countingWx,
    subscriptionPushEnabled: true,
  });
  assert.deepEqual(selfUse, { kind: "not_applicable" });
  const paused = await crisisAlerts.offerCrisisSubscribeAtBind({
    declaredMode: "parent_for_child",
    config,
    api: fakeApi(),
    wxApi: countingWx,
    subscriptionPushEnabled: false,
  });
  assert.deepEqual(paused, { kind: "paused" });
  assert.equal(prompts, 0);
});

test("bind offers the subscribe prompt before creating the binding and never gates on it; memory pins alerts", () => {
  const bindJs = fs.readFileSync(path.join(root, "pages/bind/index.js"), "utf8");
  const bindWxml = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const memoryWxml = fs.readFileSync(path.join(root, "pages/memory/index.wxml"), "utf8");
  const homeWxml = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const configJs = fs.readFileSync(path.join(root, "config.js"), "utf8");

  // DECISIONS #029: the prompt is back; #021/#030: it only offers, never gates.
  assert.match(configJs, /SUBSCRIPTION_PUSH_ENABLED\s*=\s*true/);
  assert.match(bindJs, /offerCrisisSubscribeAtBind/);
  assert.doesNotMatch(bindJs, /requireCrisisSubscribeBeforeBind|gate\.ok|BIND_SUBSCRIBE/);
  assert.match(bindJs, /createDeviceBinding/);
  assert.match(bindJs, /SUBSCRIPTION_PUSH_ENABLED/);
  const submitAt = bindJs.indexOf("async submitBinding");
  const offerAt = bindJs.indexOf("offerCrisisSubscribeAtBind", submitAt);
  const quietAt = bindJs.indexOf("confirmUnusualQuietHours", submitAt);
  const createAt = bindJs.indexOf("createDeviceBinding", submitAt);
  assert.ok(offerAt > submitAt && createAt > offerAt);
  // WeChat subscribe must start before any other await in the submit tap stack.
  assert.ok(quietAt > offerAt && quietAt < createAt);
  assert.match(bindWxml, /\{\{submitLabel\}\}/);
  assert.match(bindWxml, /subscriptionPushEnabled && crisisSubscribeOffered/);
  assert.doesNotMatch(bindWxml, /前提/);
  assert.doesNotMatch(bindJs, /offerCrisisSubscription/);
  assert.doesNotMatch(bindWxml, /危机提醒/);
  assert.doesNotMatch(memoryWxml, /危机提醒/);
  assert.doesNotMatch(homeWxml, /危机提醒/);
  assert.match(memoryWxml, /safetyBanner/);
  assert.match(memoryWxml, /subscriptionPushEnabled && crisisPush\.enabled/);
  assert.match(homeWxml, /unreadSafetyAlert/);
});

test("unread home and memory alerts stay independent of the push toggle", () => {
  const homeJs = fs.readFileSync(path.join(root, "pages/home/index.js"), "utf8");
  const memoryJs = fs.readFileSync(path.join(root, "pages/memory/index.js"), "utf8");
  const deviceJs = fs.readFileSync(path.join(root, "pages/device/index.js"), "utf8");
  assert.match(homeJs, /loadUnreadSafetyAlerts/);
  assert.match(memoryJs, /loadUnreadSafetyAlerts/);
  assert.match(homeJs, /safetySubscribeOn|optOutCrisisPushAlerts/);
  assert.doesNotMatch(memoryJs, /safetySubscribeOn|optOutCrisisPushAlerts/);
  for (const script of [deviceJs, homeJs]) {
    const optOutAt = script.indexOf("optOutCrisisPushAlerts");
    assert.ok(optOutAt > 0);
    const optOutChunk = script.slice(optOutAt, optOutAt + 500);
    assert.doesNotMatch(optOutChunk, /unreadSafetyAlert|safetyAlertId|elderAlerts\s*:\s*\[\]/);
  }
});
