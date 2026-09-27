const assert = require("node:assert/strict");
const test = require("node:test");

const {
  BIND_LINK_PREFIX,
  parseDeviceQr,
} = require("../utils/device-onboarding/qr-code");
const { OnboardingController } = require("../utils/device-onboarding/onboarding-controller");

function base64url(value) {
  return Buffer.from(value).toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function makeQr() {
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
  };
  return `memoria-bootstrap:v1:${base64url(JSON.stringify(payload))}.${base64url("signature")}`;
}

const RAW = makeQr();
const LINK = `${BIND_LINK_PREFIX}?b=${RAW}`;

test("the board's WeChat bind link carries the signed payload unchanged", () => {
  assert.equal(BIND_LINK_PREFIX, "https://aigcnice.com/memoria-bind/");
  const fromLink = parseDeviceQr(LINK);
  assert.equal(fromLink.raw_payload, RAW);
  assert.equal(fromLink.payload.device_id, "dev_01");
  // A percent-encoded payload (some scanners escape ':') decodes to the same.
  assert.equal(parseDeviceQr(`${BIND_LINK_PREFIX}?b=${encodeURIComponent(RAW)}`).raw_payload, RAW);
  // The bare payload shown by older firmware still works.
  assert.equal(parseDeviceQr(RAW).raw_payload, RAW);
});

test("every form a scanner hands back for the bind link is unwrapped", () => {
  const forms = [
    LINK,
    `  ${LINK}\n`,
    encodeURIComponent(LINK),
    `pages/device-onboarding/index?q=${encodeURIComponent(LINK)}`,
    `/pages/device-onboarding/index?scancode_time=1727430000&q=${encodeURIComponent(LINK)}`,
    LINK.replace("https://", "http://"),
    LINK.replace("aigcnice.com/", "www.aigcnice.com/"),
    LINK.replace("memoria-bind/?", "memoria-bind?"),
    `${LINK}&scancode_time=1727430000`,
  ];
  for (const form of forms) assert.equal(parseDeviceQr(form).raw_payload, RAW, form);
});

test("a foreign code is named in the error without its query", () => {
  assert.throws(
    () => parseDeviceQr("https://example.com/some/page?token=secret"),
    (error) =>
      error.code === "QR_INVALID" &&
      error.message.includes("https://example.com/some/page") &&
      !error.message.includes("secret"),
  );
});

test("a malformed bind link is refused before anything is sent", () => {
  for (const bad of [
    `${BIND_LINK_PREFIX}`,
    `${BIND_LINK_PREFIX}?x=${RAW}`,
    `${BIND_LINK_PREFIX}?b=%E0%A4%A`,
    `${BIND_LINK_PREFIX}?b=not-a-memoria-payload`,
    `${BIND_LINK_PREFIX}?b=${RAW}&b=${RAW}`,
  ]) {
    assert.throws(() => parseDeviceQr(bad), (error) => error.code === "QR_INVALID", bad);
  }
  // Another host with the same path is not our link.
  assert.throws(
    () => parseDeviceQr(`https://evil.example/memoria-bind/?b=${RAW}`),
    (error) => error.code === "QR_INVALID",
  );
});

test("introspect receives only the device payload from a bind link", async () => {
  const sent = [];
  const controller = new OnboardingController({
    apiClient: {
      introspectDeviceQr: async (request) => {
        sent.push(request.qrPayload);
        throw Object.assign(new Error("stop here"), { code: "QR_SESSION_EXPIRED" });
      },
    },
  });
  await controller.introspectQr(LINK);
  assert.deepEqual(sent, [RAW]);
  controller.dispose();
});

test("opening the onboarding page from WeChat's scanner starts with the scanned link", async () => {
  const authPath = require.resolve("../utils/auth-gate");
  const previousAuth = require.cache[authPath];
  require.cache[authPath] = { id: authPath, filename: authPath, loaded: true, exports: { requireLogin: async () => true } };
  let definition;
  global.Page = (value) => {
    definition = value;
  };
  const pagePath = require.resolve("../pages/device-onboarding/index");
  delete require.cache[pagePath];
  require(pagePath);
  const page = {
    ...definition,
    data: JSON.parse(JSON.stringify(definition.data)),
    setData(patch) {
      Object.assign(this.data, patch);
    },
  };
  page.onLoad({ q: encodeURIComponent(LINK) });
  const scanned = [];
  page._controller.introspectQr = async (value) => {
    scanned.push(value);
  };
  page._controller.resume = async () => {
    throw new Error("a linked scan must not resume an older session");
  };
  page._refreshConnectedWifi = async () => {};
  await page.onShow();
  await page.onShow(); // coming back from another page does not rescan
  assert.deepEqual(scanned, [LINK]);
  page._controller.dispose();
  delete require.cache[pagePath];
  if (previousAuth) require.cache[authPath] = previousAuth;
  else delete require.cache[authPath];
  delete global.Page;
});

test("the in-app scanner falls back to the page path WeChat returns", async () => {
  const authPath = require.resolve("../utils/auth-gate");
  const previousAuth = require.cache[authPath];
  require.cache[authPath] = { id: authPath, filename: authPath, loaded: true, exports: { requireLogin: async () => true } };
  let definition;
  global.Page = (value) => {
    definition = value;
  };
  const pagePath = require.resolve("../pages/device-onboarding/index");
  delete require.cache[pagePath];
  require(pagePath);
  const page = {
    ...definition,
    data: JSON.parse(JSON.stringify(definition.data)),
    setData(patch) {
      Object.assign(this.data, patch);
    },
  };
  page.onLoad({});
  const scanned = [];
  page._controller.introspectQr = async (value) => {
    scanned.push(value);
  };
  const previousWx = global.wx;
  global.wx = {
    scanCode: ({ success, complete }) => {
      success({
        result: "unrecognised text",
        path: `pages/device-onboarding/index?q=${encodeURIComponent(LINK)}`,
      });
      complete?.();
    },
  };
  page.scanQr();
  assert.deepEqual(scanned, [`pages/device-onboarding/index?q=${encodeURIComponent(LINK)}`]);
  page._controller.dispose();
  delete require.cache[pagePath];
  if (previousAuth) require.cache[authPath] = previousAuth;
  else delete require.cache[authPath];
  if (previousWx === undefined) delete global.wx;
  else global.wx = previousWx;
  delete global.Page;
});
