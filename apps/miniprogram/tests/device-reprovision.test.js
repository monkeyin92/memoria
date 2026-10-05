const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  OnboardingController,
  normalizeIntrospectResponse,
  normalizeOnboardingSession,
  errorMessage,
  STORAGE_KEY,
} = require("../utils/device-onboarding");

const root = path.join(__dirname, "..");

function base64url(value) {
  return Buffer.from(value).toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function makeQr() {
  const payload = {
    typ: "memoria-device-bootstrap",
    ver: 1,
    device_id: "dev_01",
    bootstrap_nonce: "nonce_new_place",
    ble_name: "MEM-ABCD",
    ble_service_uuid: "12345678-1234-4234-8234-1234567890ab",
    certificate_id: "cert_01",
    provisioning_protocol: "memoria-provisioning/1",
    firmware_version: "0.1.0",
    pop: "pop_01",
  };
  return `memoria-bootstrap:v1:${base64url(JSON.stringify(payload))}.${base64url("signature")}`;
}

function makeSession(overrides = {}) {
  return {
    onboarding_session_id: "onb_reprovision",
    purpose: "reprovision",
    state: "qr_verified",
    state_version: 1,
    activation_version: 1,
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    device: {
      device_id: "dev_01",
      display_tail: "ABCD",
      model: "memoria-atk-dnesp32s3-v1",
      firmware_version: "0.1.0",
      claim_status: "bound",
    },
    provisioning: {
      transport: "ble",
      ble_name: "MEM-ABCD",
      service_uuid: "12345678-1234-4234-8234-1234567890ab",
      protocol_version: 1,
    },
    mobile_nonce: "mobile_nonce",
    ...overrides,
  };
}

function withStorage(fn) {
  return async () => {
    const previousWx = global.wx;
    const storage = new Map();
    global.wx = {
      setStorageSync(key, value) { storage.set(key, value); },
      getStorageSync(key) { return storage.get(key) || ""; },
      removeStorageSync(key) { storage.delete(key); },
    };
    try {
      await fn(storage);
    } finally {
      if (previousWx === undefined) delete global.wx;
      else global.wx = previousWx;
    }
  };
}

function fakeApi(sessions, calls) {
  return {
    introspectDeviceQr: async () => {
      calls.push("introspect");
      return sessions.introspect;
    },
    getOnboardingSession: async () => {
      calls.push("session");
      return sessions.next.shift();
    },
    reserveDeviceClaim: async () => {
      calls.push("claim");
      throw new Error("reprovision must never claim");
    },
    getDeviceClaim: async () => {
      calls.push("get-claim");
      throw new Error("reprovision has no claim");
    },
  };
}

test("contracts carry the session purpose and default old servers to onboarding", () => {
  assert.equal(normalizeIntrospectResponse(makeSession()).purpose, "reprovision");
  const { purpose: _omitted, ...legacy } = makeSession({ device: { ...makeSession().device, claim_status: "unclaimed" } });
  assert.equal(normalizeIntrospectResponse(legacy).purpose, "onboarding");
  assert.equal(normalizeOnboardingSession(makeSession({ state: "device_online" })).purpose, "reprovision");
  assert.throws(() => normalizeOnboardingSession(makeSession({ purpose: "transfer" })), /purpose/);
});

test("reprovision finishes at device_online without claim, binding, or activation", withStorage(async (storage) => {
  const calls = [];
  const writes = [];
  const controller = new OnboardingController({
    mode: "reprovision",
    apiClient: fakeApi(
      {
        introspect: makeSession(),
        next: [makeSession({ state: "device_online", state_version: 3 })],
      },
      calls,
    ),
  });
  assert.equal(controller.snapshot().reprovision, true);
  await controller.introspectQr(makeQr());
  assert.equal(controller.state, "device_verified");
  assert.equal(storage.get(STORAGE_KEY), "onb_reprovision");

  // The BLE session itself is covered by the add-device tests; reprovision
  // reuses it unchanged, so a connected transport is stood in here.
  controller._transport = {
    writeWifiCredentials: async (credentials) => writes.push(credentials.ssid),
    dispose() {},
  };
  controller._setState("wifi", { force: true });
  assert.equal(await controller.provisionWifi({ ssid: "new-home", password: "secret-pass" }), true);

  const snapshot = controller.snapshot();
  assert.deepEqual(writes, ["new-home"]);
  assert.equal(snapshot.state, "complete");
  assert.equal(snapshot.stateLabel, "网络已更新");
  assert.equal(snapshot.claim, null);
  assert.equal(snapshot.activation, null);
  assert.equal(storage.has(STORAGE_KEY), false);
  assert.equal(await controller.reserveClaim(), null);
  assert.equal(controller.beginInitialize(), false);
  assert.deepEqual(calls, ["introspect", "session"]);
  controller.dispose();
}));

test("reprovision progress is not filled in by the device's existing activation", () => {
  const controller = new OnboardingController({ mode: "reprovision" });
  controller._session = makeSession({ state: "wifi_configuring", activation_status: "ready_for_conversation" });
  controller._setState("progress", { force: true });
  assert.equal(controller.snapshot().progressIndex, 0);
  controller.dispose();
});

test("reprovision entry refuses a robot that is not bound to this account", withStorage(async (storage) => {
  const calls = [];
  const controller = new OnboardingController({
    mode: "reprovision",
    apiClient: fakeApi(
      { introspect: makeSession({ purpose: "onboarding", device: { ...makeSession().device, claim_status: "unclaimed" } }), next: [] },
      calls,
    ),
  });
  assert.equal(await controller.introspectQr(makeQr()), null);
  const snapshot = controller.snapshot();
  assert.equal(snapshot.state, "scan");
  assert.equal(snapshot.errorCode, "REPROVISION_DEVICE_NOT_BOUND");
  assert.match(snapshot.error, /添加其他设备/);
  assert.equal(snapshot.session, null);
  assert.equal(storage.has(STORAGE_KEY), false);
  controller.dispose();
}));

test("add-device entry follows the server when the owner scans their own bound robot", withStorage(async () => {
  const calls = [];
  const controller = new OnboardingController({
    apiClient: fakeApi(
      { introspect: makeSession(), next: [makeSession({ state: "device_online" })] },
      calls,
    ),
  });
  assert.equal(controller.snapshot().reprovision, false);
  await controller.introspectQr(makeQr());
  assert.equal(controller.snapshot().reprovision, true);
  await controller.refreshSession();
  assert.equal(controller.state, "complete");
  assert.equal(calls.includes("claim"), false);
  controller.dispose();
}));

test("add-device flow still stops at progress for an onboarding session that is online", withStorage(async () => {
  const controller = new OnboardingController({
    apiClient: {
      getOnboardingSession: async () =>
        makeSession({ purpose: "onboarding", state: "device_online", device: { ...makeSession().device, claim_status: "unclaimed" } }),
    },
  });
  controller._session = makeSession({ purpose: "onboarding" });
  await controller.refreshSession();
  assert.equal(controller.state, "progress");
  assert.equal(controller.snapshot().progressIndex, 6);
  controller.dispose();
}));

test("resuming a finished reprovision lands on complete and clears the stored session", withStorage(async (storage) => {
  const calls = [];
  const controller = new OnboardingController({
    apiClient: fakeApi({ next: [makeSession({ state: "device_online" })] }, calls),
  });
  await controller.resume("onb_reprovision");
  assert.equal(controller.state, "complete");
  assert.equal(storage.has(STORAGE_KEY), false);
  assert.deepEqual(calls, ["session"]);
  controller.dispose();
}));

test("DEVICE_ALREADY_BOUND now speaks to accounts the robot is not bound to", () => {
  const message = errorMessage({ code: "DEVICE_ALREADY_BOUND" });
  assert.match(message, /其他账号/);
  assert.doesNotMatch(message, /如果是你自己的设备/);
});

test("onboarding page passes its entry mode and hides claim steps while reprovisioning", () => {
  const script = fs.readFileSync(path.join(root, "pages/device-onboarding/index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages/device-onboarding/index.wxml"), "utf8");
  assert.match(script, /new OnboardingController\(\{\s*mode: this\._mode,/);
  assert.match(template, /<block wx:if="\{\{!reprovision\}\}">[\s\S]*确认归属[\s\S]*激活完成[\s\S]*<\/block>/);
  assert.match(template, /network.phase === 'online' && !reprovision/);
});
