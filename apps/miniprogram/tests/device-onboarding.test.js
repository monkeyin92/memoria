const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const api = require("../utils/api");
const {
  OnboardingController,
  WxBleAdapter,
  FrameReassembler,
  SensitiveBuffer,
  fragmentFrame,
  endpointUuid,
  endpointUuidCandidates,
  parseDeviceQr,
  isActivationReady,
} = require("../utils/device-onboarding");

const root = path.join(__dirname, "..");

function base64url(value) {
  return Buffer.from(value).toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function makeQr(overrides = {}) {
  const payload = {
    typ: "memoria-device-bootstrap",
    ver: 1,
    device_id: "dev_01",
    bootstrap_nonce: "nonce_01",
    ble_name: "MEM-ABCD",
    ble_service_uuid: "12345678-1234-4234-8234-1234567890ab",
    certificate_id: "cert_01",
    provisioning_protocol: "memoria-provisioning/1",
    firmware_version: "0.1.0",
    pop: "pop_01",
    ...overrides,
  };
  return `memoria-bootstrap:v1:${base64url(JSON.stringify(payload))}.${base64url("signature")}`;
}

function introspectResponse(overrides = {}) {
  return {
    onboarding_session_id: "onb_01",
    state: "qr_verified",
    state_version: 2,
    activation_version: 2,
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    device: {
      device_id: "dev_01",
      display_tail: "ABCD",
      model: "memoria-atk-dnesp32s3-v1",
      firmware_version: "0.1.0",
      claim_status: "unclaimed",
    },
    provisioning: {
      transport: "ble",
      ble_name: "MEM-ABCD",
      service_uuid: "12345678-1234-4234-8234-1234567890ab",
      protocol_version: 1,
    },
    mobile_nonce: "mobile_nonce",
    claim_id: "claim_01",
    binding_id: "binding_01",
    activation_status: "device_acknowledged",
    ...overrides,
  };
}

function makeSession(overrides = {}) {
  return {
    onboarding_session_id: "onb_01",
    state: "claim_reserved",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    state_version: 2,
    activation_version: 2,
    device: {
      device_id: "dev_01",
      display_tail: "ABCD",
      model: "memoria-atk-dnesp32s3-v1",
      firmware_version: "0.1.0",
      claim_status: "unclaimed",
    },
    provisioning: {
      transport: "ble",
      ble_name: "MEM-ABCD",
      service_uuid: "12345678-1234-4234-8234-1234567890ab",
      protocol_version: 1,
    },
    claim_id: "claim_01",
    ...overrides,
  };
}

function makeClaim(overrides = {}) {
  return {
    claim_id: "claim_01",
    device_id: "dev_01",
    onboarding_session_id: "onb_01",
    status: "reserved",
    expires_at: new Date(Date.now() + 600_000).toISOString(),
    ...overrides,
  };
}

test("QR parser accepts the versioned shape and rejects unknown/versioned payloads", () => {
  const raw = makeQr();
  const parsed = parseDeviceQr(raw);
  assert.equal(parsed.raw_payload, raw);
  assert.equal(parsed.payload.device_id, "dev_01");
  assert.equal(parsed.server_signature_verification_required, true);

  assert.throws(
    () => parseDeviceQr(makeQr({ ver: 2 })),
    (error) => error.code === "PROTOCOL_UNSUPPORTED",
  );
  assert.throws(
    () => parseDeviceQr(makeQr({ unexpected: true })),
    (error) => error.code === "QR_INVALID",
  );
});

test("packet framing fragments at the BLE MTU and reassembles with integrity checks", () => {
  const frames = fragmentFrame("Memoria provisioning payload", { mtu: 20, messageId: 17 });
  assert.ok(frames.length > 1);
  const reassembler = new FrameReassembler();
  let merged = null;
  frames.forEach((frame) => {
    merged = reassembler.push(frame) || merged;
  });
  assert.equal(Buffer.from(merged).toString(), "Memoria provisioning payload");

  const tampered = new Uint8Array(frames[0]);
  tampered[tampered.length - 1] ^= 1;
  assert.throws(() => new FrameReassembler().push(tampered), /完整性校验失败/);
});

test("default provisioning transport fails closed before any Wi-Fi write without audited crypto", async () => {
  const { createProvisioningTransport } = require("../utils/device-onboarding/provisioning-transport");
  let writes = 0;
  const transport = createProvisioningTransport({
    adapter: { write: async () => { writes += 1; } },
    deviceId: "dev_01",
    serviceId: "service",
    writeCharacteristicId: "write",
    notifyCharacteristicId: "notify",
    epoch: 1,
  });
  await assert.rejects(
    transport.establishSecureSession(),
    (error) => error.code === "PROTOCOL_UNSUPPORTED",
  );
  await assert.rejects(
    transport.writeWifiCredentials({ ssid: "家庭网络", password: "not-sent" }),
    (error) => error.code === "BLE_REAUTH_REQUIRED",
  );
  assert.equal(writes, 0);
});

test("provisioning asks for a fresh QR when the in-memory BLE session was released", async () => {
  const { OnboardingController } = require("../utils/device-onboarding/onboarding-controller");
  const controller = new OnboardingController();
  controller._session = makeSession({ state: "wifi_configuring" });
  const result = await controller.provisionWifi({ ssid: "915", password: "password" });
  assert.equal(result, false);
  assert.equal(controller.snapshot().state, "scan");
  assert.equal(controller.snapshot().errorCode, "BLE_REAUTH_REQUIRED");
  controller.dispose();
});

test("Protocomm Security 1 completes the X25519, PoP, and AES-CTR proof exchange", async () => {
  const { Aes256Ctr, nacl, sha256 } = require("../utils/device-onboarding/crypto");
  const { Security1Adapter, readFields } = require("../utils/device-onboarding/protocomm-codec");
  const concat = (...parts) => {
    const output = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    parts.forEach((part) => { output.set(part, offset); offset += part.length; });
    return output;
  };
  const varint = (value) => {
    const output = [];
    let current = value;
    do {
      const byte = current & 0x7f;
      current = Math.floor(current / 128);
      output.push(byte | (current ? 0x80 : 0));
    } while (current);
    return Uint8Array.from(output);
  };
  const fieldBytes = (field, value) => concat(varint((field << 3) | 2), varint(value.length), value);
  const fieldVarint = (field, value) => concat(varint(field << 3), varint(value));
  const response0 = (devicePublicKey, deviceRandom) => {
    const response = concat(fieldBytes(2, devicePublicKey), fieldBytes(3, deviceRandom));
    const sec = concat(fieldVarint(1, 1), fieldBytes(21, response));
    return concat(fieldVarint(2, 1), fieldBytes(11, sec));
  };
  const response1 = (deviceVerifyData) => {
    const response = fieldBytes(3, deviceVerifyData);
    const sec = concat(fieldVarint(1, 3), fieldBytes(23, response));
    return concat(fieldVarint(2, 1), fieldBytes(11, sec));
  };
  const serverSecret = Uint8Array.from({ length: 32 }, (_, index) => index + 1);
  const serverKeyPair = nacl.box.keyPair.fromSecretKey(serverSecret);
  const deviceRandom = Uint8Array.from({ length: 16 }, (_, index) => 0xa0 + index);
  let request = null;
  let clientPublicKey = null;
  let serverCipher = null;
  const adapter = new Security1Adapter({ pop: "pop" });
  const transportAdapter = {
    async write({ value }) { request = new Uint8Array(value); },
    async read() {
      const outer = readFields(request);
      const sec = readFields(outer.get(11)[0].value);
      const message = sec.get(1)[0].value;
      if (message === 0) {
        const command = readFields(sec.get(20)[0].value);
        clientPublicKey = command.get(1)[0].value;
        const shared = nacl.scalarMult(serverSecret, clientPublicKey);
        const popKey = sha256("pop");
        for (let index = 0; index < shared.length; index += 1) shared[index] ^= popKey[index];
        serverCipher = new Aes256Ctr(shared, deviceRandom);
        return response0(serverKeyPair.publicKey, deviceRandom).buffer;
      }
      assert.equal(message, 2);
      const command = readFields(sec.get(22)[0].value);
      const encryptedClientKey = command.get(2)[0].value;
      const decryptedClientKey = serverCipher.update(encryptedClientKey);
      assert.deepEqual(decryptedClientKey, serverKeyPair.publicKey);
      return response1(serverCipher.update(clientPublicKey)).buffer;
    },
  };
  const session = await adapter.establishSession({
    adapter: transportAdapter,
    deviceId: "dev_01",
    serviceId: "service",
    endpointCharacteristics: { "proto-session": { writeCharacteristicId: "session" } },
    epoch: 1,
  });
  assert.equal(session.authenticated, true);
  session.dispose();
});

function makeBleWx({
  serviceUuid = "12345678-1234-4234-8234-1234567890ab",
  characteristicUuid = endpointUuid,
  readValue = null,
} = {}) {
  const listeners = {
    found: new Set(),
    connection: new Set(),
    value: new Set(),
    adapter: new Set(),
  };
  const calls = [];
  const discoveryOptions = [];
  const endpointIds = [0xff50, 0xff51, 0xff52, 0xff53, 0xff54, 0xff55];
  const wxApi = {
    calls,
    listeners,
    openBluetoothAdapter(options) { calls.push("open"); options.success(); },
    startBluetoothDevicesDiscovery(options) { calls.push("discover"); discoveryOptions.push(options); options.success(); },
    stopBluetoothDevicesDiscovery(options) { calls.push("stop-discovery"); options.complete?.(); },
    createBLEConnection(options) { calls.push("connect"); options.success(); },
    getBLEDeviceServices(options) {
      calls.push("services");
      options.success({ services: [{ uuid: serviceUuid }] });
    },
    getBLEDeviceCharacteristics(options) {
      calls.push("characteristics");
      options.success({
        characteristics: [
          ...endpointIds.map((id) => ({
            uuid: characteristicUuid(serviceUuid, id),
            properties: { write: true, read: true },
          })),
        ],
      });
    },
    notifyBLECharacteristicValueChanged(options) { calls.push("notify"); options.success(); },
    writeBLECharacteristicValue(options) { calls.push("write"); options.success(); },
    readBLECharacteristicValue(options) {
      calls.push("read");
      options.success(readValue || {});
    },
    closeBLEConnection(options) { calls.push("close"); options.complete?.(); },
    closeBluetoothAdapter(options) { calls.push("close-adapter"); options.complete?.(); },
    onBluetoothDeviceFound(callback) { listeners.found.add(callback); },
    offBluetoothDeviceFound(callback) { listeners.found.delete(callback); calls.push("off-found"); },
    onBLEConnectionStateChange(callback) { listeners.connection.add(callback); },
    offBLEConnectionStateChange(callback) { listeners.connection.delete(callback); calls.push("off-connection"); },
    onBLECharacteristicValueChange(callback) { listeners.value.add(callback); },
    offBLECharacteristicValueChange(callback) { listeners.value.delete(callback); calls.push("off-value"); },
    onBluetoothAdapterStateChange(callback) { listeners.adapter.add(callback); },
    offBluetoothAdapterStateChange(callback) { listeners.adapter.delete(callback); calls.push("off-adapter"); },
  };
  wxApi.discoveryOptions = discoveryOptions;
  return wxApi;
}

test("BLE adapter calls the real wx lifecycle and clears listeners on dispose", async () => {
  const wxApi = makeBleWx();
  const adapter = new WxBleAdapter(wxApi);
  const epoch = await adapter.open();
  let foundCallback;
  const discovery = adapter.discover({
    serviceUuid: "12345678-1234-4234-8234-1234567890ab",
    bleName: "MEM-ABCD",
    displayTail: "ABCD",
    timeoutMs: 1000,
    epoch,
  });
  foundCallback = [...wxApi.listeners.found][0];
  foundCallback({ devices: [{ deviceId: "wx-device-1", name: "MEM-ABCD" }] });
  await discovery;
  assert.deepEqual(wxApi.discoveryOptions[0].services, []);
  const connection = await adapter.connect("wx-device-1", {
    serviceUuid: "12345678-1234-4234-8234-1234567890ab",
    epoch,
  });
  await adapter.write({
    ...connection,
    value: new Uint8Array([1, 2, 3]).buffer,
    epoch,
  });
  assert.deepEqual(
    ["open", "discover", "off-found", "stop-discovery", "connect", "services", "characteristics", "off-connection", "write"],
    wxApi.calls.slice(0, 9),
  );

  let valueEvents = 0;
  adapter.subscribeValue(() => { valueEvents += 1; }, { epoch });
  [...wxApi.listeners.value][0]({ value: new ArrayBuffer(0) });
  assert.equal(valueEvents, 1);
  adapter.dispose();
  assert.equal(wxApi.listeners.value.size, 0);
  assert.ok(wxApi.calls.includes("off-value"));
  assert.ok(wxApi.calls.includes("close-adapter"));
  [...wxApi.listeners.value].forEach((callback) => callback({ value: new ArrayBuffer(0) }));
  assert.equal(valueEvents, 1);
});

test("BLE adapter uses the iOS negotiated ATT payload as the conservative default", async () => {
  const wxApi = makeBleWx();
  wxApi.getSystemInfoSync = () => ({ platform: "ios" });
  const adapter = new WxBleAdapter(wxApi);
  const epoch = await adapter.open();
  const connection = await adapter.connect("wx-device-1", {
    serviceUuid: "12345678-1234-4234-8234-1234567890ab",
    epoch,
  });
  assert.equal(connection.mtu, 182);
  adapter.dispose();
});

test("BLE adapter ignores empty read results until the non-empty GATT value event arrives", async () => {
  const wxApi = makeBleWx({ readValue: { value: new ArrayBuffer(0) } });
  const adapter = new WxBleAdapter(wxApi);
  const epoch = await adapter.open();
  const promise = adapter.read({
    deviceId: "wx-device-1",
    serviceId: "12345678-1234-4234-8234-1234567890ab",
    characteristicId: "session",
    epoch,
    timeoutMs: 1000,
  });
  await new Promise((resolve) => setImmediate(resolve));
  const event = [...wxApi.listeners.value][0];
  event({
    deviceId: "wx-device-1",
    characteristicId: "session",
    value: new Uint8Array([1, 2, 3]).buffer,
  });
  assert.deepEqual(new Uint8Array(await promise), new Uint8Array([1, 2, 3]));
  adapter.dispose();
});

test("BLE adapter accepts CoreBluetooth byte-reversed Protocomm endpoint UUIDs", () => {
  assert.deepEqual(
    endpointUuidCandidates("3D981E4A-31EB-42B4-8A68-75BD8D3BD521", 0xff50),
    [
      "3d981e4a-31eb-42b4-8a68-75bd50ffd521",
      "3d98ff50-31eb-42b4-8a68-75bd8d3bd521",
    ],
  );
});

test("BLE adapter resolves the real CoreBluetooth GATT characteristic set", async () => {
  const serviceUuid = "3D981E4A-31EB-42B4-8A68-75BD8D3BD521";
  const wxApi = makeBleWx({
    serviceUuid,
    characteristicUuid: (service, id) => endpointUuidCandidates(service, id)[1],
  });
  const adapter = new WxBleAdapter(wxApi);
  const epoch = await adapter.open();
  const connection = await adapter.connect("wx-device-1", { serviceUuid, epoch });
  assert.equal(connection.endpointCharacteristics["proto-session"].writeCharacteristicId,
    "3d98ff51-31eb-42b4-8a68-75bd8d3bd521");
  adapter.dispose();
});

test("BLE discovery invalidates a previous attempt and drops its late event", async () => {
  const wxApi = makeBleWx();
  const adapter = new WxBleAdapter(wxApi);
  const epoch = await adapter.open();
  const pending = adapter.discover({
    serviceUuid: "12345678-1234-4234-8234-1234567890ab",
    bleName: "MEM-ABCD",
    displayTail: "ABCD",
    timeoutMs: 1000,
    epoch,
  });
  const lateCallback = [...wxApi.listeners.found][0];
  adapter.beginAttempt();
  lateCallback({ devices: [{ deviceId: "late", name: "MEM-ABCD" }] });
  await assert.rejects(pending, (error) => error.code === "BLE_ATTEMPT_STALE");
  adapter.dispose();
});

test("SensitiveBuffer is cleared and never retains Wi-Fi password bytes", () => {
  const buffer = new SensitiveBuffer();
  buffer.setText("家庭 Wi-Fi 密码");
  assert.ok(buffer.length > 0);
  buffer.clear();
  assert.equal(buffer.length, 0);
  assert.equal(buffer.getText(), "");
});

test("resume fences the second getDeviceClaim await after pause/dispose", async () => {
  const previousWx = global.wx;
  global.wx = {
    setStorageSync() {},
    getStorageSync() { return ""; },
    removeStorageSync() {},
  };
  let resolveClaim;
  let claimStarted;
  const apiClient = {
    getOnboardingSession: async () => makeSession(),
    getDeviceClaim: () => new Promise((resolve) => {
      resolveClaim = resolve;
      claimStarted = true;
    }),
  };
  const controller = new OnboardingController({ apiClient });
  const resumePromise = controller.resume("onb_01");
  for (let i = 0; i < 10 && !claimStarted; i += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(claimStarted, true);
  controller.pause();
  resolveClaim(makeClaim());
  assert.equal(await resumePromise, null);
  assert.equal(controller.claim, null);
  controller.dispose();
  if (previousWx === undefined) delete global.wx;
  else global.wx = previousWx;
});

test("expired session cannot reserve a claim", async () => {
  let reserveCalls = 0;
  const controller = new OnboardingController({
    apiClient: {
      reserveDeviceClaim: async () => {
        reserveCalls += 1;
        return makeClaim();
      },
    },
    now: () => Date.parse("2026-08-11T10:00:00Z"),
  });
  controller._session = makeSession({ expires_at: "2026-08-11T09:59:59Z" });
  const claim = await controller.reserveClaim();
  assert.equal(claim, null);
  assert.equal(reserveCalls, 0);
  assert.equal(controller.snapshot().errorCode, "CLAIM_EXPIRED");
  controller.dispose();
});

test("claim retries reuse the onboarding-session idempotency key", async () => {
  const requests = [];
  const controller = new OnboardingController({
    apiClient: {
      reserveDeviceClaim: async (request) => {
        requests.push(request);
        return makeClaim();
      },
    },
  });
  controller._session = makeSession({ state: "device_online" });
  await controller.reserveClaim();
  await controller.reserveClaim();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].idempotencyKey, "claim-onb_01");
  assert.equal(requests[1].idempotencyKey, requests[0].idempotencyKey);
  controller.dispose();
});

test("late activation ACK cannot be regressed by an older response", async () => {
  const statuses = [
    { device_id: "dev_01", status: "device_applied" },
    { device_id: "dev_01", status: "device_acknowledged" },
    { device_id: "dev_01", status: "device_applied" },
  ];
  const controller = new OnboardingController({
    apiClient: {
      getActivationStatus: async () => statuses.shift(),
    },
  });
  controller.onBindingCreated({ device_id: "dev_01", binding_id: "binding_01" });
  await new Promise((resolve) => setImmediate(resolve));
  await controller.refreshActivation();
  assert.equal(controller.snapshot().activation.status, "device_acknowledged");
  assert.equal(controller.snapshot().state, "complete");
  await controller.refreshActivation();
  assert.equal(controller.snapshot().activation.status, "device_acknowledged");
  assert.equal(isActivationReady(controller.snapshot().activation.status), true);
  controller.dispose();
});

test("onboarding API sends raw QR and uses the planned paths with strict responses", async () => {
  const previousWx = global.wx;
  const previousGetApp = global.getApp;
  const calls = [];
  const session = makeSession({ state: "wifi_connected", claim_id: null });
  const claim = makeClaim();
  global.getApp = () => ({
    globalData: {
      identity: { user_id: "person_owner" },
      accessToken: "token",
      accessTokenExpiresAt: Date.now() + 60_000,
    },
  });
  global.wx = {
    getAccountInfoSync: () => ({ miniProgram: { version: "0.1.0" } }),
    getSystemInfoSync: () => ({ SDKVersion: "3.8.0" }),
    request(options) {
      const url = new URL(options.url);
      calls.push({ path: url.pathname, method: options.method, data: options.data });
      if (url.pathname === "/memoria-api/v1/device-bootstrap/introspect") {
        options.success({ statusCode: 200, data: introspectResponse() });
      } else if (url.pathname === "/memoria-api/v1/device-bootstrap/onb_01") {
        options.success({ statusCode: 200, data: session });
      } else if (url.pathname.endsWith("/cancel")) {
        options.success({ statusCode: 200, data: makeSession({ state: "cancelled", claim_id: null }) });
      } else if (url.pathname === "/memoria-api/v1/device-claims") {
        options.success({ statusCode: 200, data: claim });
      } else if (url.pathname === "/memoria-api/v1/device-claims/claim_01") {
        options.success({ statusCode: 200, data: claim });
      } else if (url.pathname === "/memoria-api/v1/device-activations/dev_01") {
        options.success({ statusCode: 200, data: { device_id: "dev_01", status: "device_acknowledged" } });
      } else {
        options.fail({ errMsg: "unexpected path" });
      }
    },
  };
  const rawQr = ` ${makeQr()} `;
  const introspected = await api.introspectDeviceQr({ qrPayload: rawQr, clientOnboardingId: "client_onb_01" });
  const fetchedSession = await api.getOnboardingSession("onb_01");
  await api.cancelOnboardingSession("onb_01");
  await api.reserveDeviceClaim({
    onboardingSessionId: "onb_01",
    deviceId: "dev_01",
    idempotencyKey: "claim-idem-01",
    expectedStateVersion: 2,
  });
  await api.getDeviceClaim("claim_01");
  await api.getActivationStatus("dev_01");
  assert.equal(introspected.activation_version, 2);
  assert.equal(introspected.activation_status, "device_acknowledged");
  assert.equal(introspected.claim_id, "claim_01");
  assert.equal(introspected.binding_id, "binding_01");
  assert.equal(fetchedSession.activation_version, 2);
  assert.deepEqual(
    calls.map((call) => `${call.method} ${call.path}`),
    [
      "POST /memoria-api/v1/device-bootstrap/introspect",
      "GET /memoria-api/v1/device-bootstrap/onb_01",
      "POST /memoria-api/v1/device-bootstrap/onb_01/cancel",
      "POST /memoria-api/v1/device-claims",
      "GET /memoria-api/v1/device-claims/claim_01",
      "GET /memoria-api/v1/device-activations/dev_01",
    ],
  );
  assert.equal(calls[0].data.qr_payload, rawQr);
  assert.equal(calls[3].data.onboarding_session_id, "onb_01");
  assert.equal(calls[3].data.device_id, "dev_01");
  assert.equal(calls[3].data.expected_state_version, 2);
  assert.equal(JSON.stringify(calls).includes("not-sent"), false);
  if (previousWx === undefined) delete global.wx;
  else global.wx = previousWx;
  if (previousGetApp === undefined) delete global.getApp;
  else global.getApp = previousGetApp;
});

test("bind page removes scanner/token source and onboarding clears password on hide", () => {
  const bindScript = fs.readFileSync(path.join(root, "pages/bind/index.js"), "utf8");
  const bindTemplate = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const onboardingScript = fs.readFileSync(path.join(root, "pages/device-onboarding/index.js"), "utf8");
  const onboardingStyles = fs.readFileSync(path.join(root, "pages/device-onboarding/index.wxss"), "utf8");
  assert.doesNotMatch(bindScript, /device_claim_token|scanDeviceCode|deviceClaimToken/);
  assert.doesNotMatch(bindTemplate, /设备码|bind-token-input|scanDeviceCode/);
  assert.match(onboardingScript, /onHide\(\)[\s\S]*_clearWifiPassword\(\)/);
  assert.match(onboardingStyles, /\.wifi-list\s*\{[^}]*overflow-y:\s*scroll/s);
});
