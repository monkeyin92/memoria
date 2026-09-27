const assert = require("node:assert/strict");
const test = require("node:test");

const { OnboardingController } = require("../utils/device-onboarding/onboarding-controller");

function session(state, extra = {}) {
  return {
    onboarding_session_id: "onb_01",
    state,
    purpose: "onboarding",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    state_version: 3,
    device: { device_id: "dev_01", display_tail: "ABCD" },
    provisioning: { transport: "ble", ble_name: "MEM-ABCD", service_uuid: "u", protocol_version: 1 },
    ...extra,
  };
}

// A controller mid-flow (BLE session up, on the Wi-Fi form) with a fake
// clock: every poll advances time by the sleep it asked for.
function controllerWith({ joinedAfterPolls = Infinity, onlineAfterPolls = Infinity, purpose = "onboarding" }) {
  let clock = 0;
  let polls = 0;
  const calls = { claims: 0, writes: [], statusReads: 0 };
  const controller = new OnboardingController({
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    apiClient: {
      getOnboardingSession: async () => {
        polls += 1;
        return session(polls >= onlineAfterPolls ? "device_online" : "ble_connecting", { purpose });
      },
      reserveDeviceClaim: async () => {
        calls.claims += 1;
        throw Object.assign(new Error("stop at claim"), { code: "CLAIM_CONFLICT" });
      },
    },
  });
  controller._session = session("ble_connecting", { purpose });
  controller._state = "wifi";
  controller._transport = {
    writeWifiCredentials: async (value) => calls.writes.push(value.ssid),
    request: async (endpoint) => {
      assert.equal(endpoint, "prov-status");
      calls.statusReads += 1;
      return { connected: calls.statusReads >= joinedAfterPolls, ip: "", ssid: "Home" };
    },
  };
  return { controller, calls };
}

test("the robot joining Wi-Fi and coming online moves straight on to the claim", async () => {
  const { controller, calls } = controllerWith({ joinedAfterPolls: 2, onlineAfterPolls: 4 });
  const seen = [];
  controller.onChange = (snapshot) => snapshot.network && seen.push(snapshot.network.phase);
  const ok = await controller.provisionWifi({ ssid: "Home", password: "secret-pass" });
  assert.equal(ok, true);
  assert.deepEqual(calls.writes, ["Home"]);
  assert.ok(seen.includes("joining"));
  assert.ok(seen.includes("cloud"));
  assert.equal(controller.snapshot().network.phase, "online");
  assert.equal(calls.claims, 1);
  controller.dispose();
});

test("no Wi-Fi join within 45 s is reported with the network name", async () => {
  const { controller } = controllerWith({});
  const ok = await controller.provisionWifi({ ssid: "Home", password: "wrong-pass" });
  assert.equal(ok, false);
  const network = controller.snapshot().network;
  assert.equal(network.phase, "failed");
  assert.equal(network.failure, "wifi_join");
  assert.equal(network.ssid, "Home");
  assert.ok(network.elapsedS >= 45 && network.elapsedS < 50);
  controller.dispose();
});

test("joined Wi-Fi but no cloud within 90 s is a separate failure", async () => {
  const { controller } = controllerWith({ joinedAfterPolls: 1 });
  const ok = await controller.provisionWifi({ ssid: "Home", password: "secret-pass" });
  assert.equal(ok, false);
  const network = controller.snapshot().network;
  assert.equal(network.failure, "cloud");
  assert.equal(network.joined, true);
  assert.ok(network.elapsedS >= 90);
  controller.dispose();
});

test("the password is cleared before the watch starts", async () => {
  const { controller } = controllerWith({ onlineAfterPolls: 1 });
  let clearedBeforePoll = null;
  const original = controller.api.getOnboardingSession;
  controller.api.getOnboardingSession = async () => {
    clearedBeforePoll = controller._wifiPassword.getText() === "";
    return original();
  };
  await controller.provisionWifi({ ssid: "Home", password: "secret-pass" });
  assert.equal(clearedBeforePoll, true);
  controller.dispose();
});

test("a reprovision ends as soon as the robot is back online", async () => {
  const { controller, calls } = controllerWith({ onlineAfterPolls: 2, purpose: "reprovision" });
  const ok = await controller.provisionWifi({ ssid: "Home", password: "secret-pass" });
  assert.equal(ok, true);
  assert.equal(controller.state, "complete");
  assert.equal(calls.claims, 0);
  controller.dispose();
});

test("retrying Wi-Fi reuses a live BLE session", async () => {
  const { controller } = controllerWith({});
  await controller.provisionWifi({ ssid: "Home", password: "wrong-pass" });
  let scans = 0;
  controller._transport.request = async (endpoint) => {
    if (endpoint === "prov-scan") scans += 1;
    return { networks: [] };
  };
  await controller.retryWifi();
  assert.equal(controller.state, "wifi");
  assert.equal(controller.snapshot().network, null);
  assert.equal(scans, 1);
  controller.dispose();
});
