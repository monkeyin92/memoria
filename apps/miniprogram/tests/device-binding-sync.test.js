const assert = require("node:assert/strict");
const { beforeEach, test } = require("node:test");

const binding = require("../utils/device-binding");
const { canonicalManifest } = require("./manifest-fixtures");

const storage = {};
const pendingRequests = [];
const app = {
  globalData: {},
  clearAuthenticatedIdentity() {
    this.globalData.authEpoch += 1;
    this.globalData.identity = null;
    this.globalData.accessToken = "";
    this.globalData.accessTokenExpiresAt = 0;
  },
};

global.wx = {
  getStorageSync: (key) => storage[key],
  setStorageSync: (key, value) => {
    storage[key] = value;
  },
  removeStorageSync: (key) => {
    delete storage[key];
  },
  request(options) {
    pendingRequests.push(options);
  },
};
global.getApp = () => app;

const apiPath = require.resolve("../utils/api");
delete require.cache[apiPath];
const api = require("../utils/api");

const RUNTIME_PROFILE_KEY = "memoria:miniprogram:device:runtime-profile";

function manifest(id, deviceId, overrides = {}) {
  return canonicalManifest({
    binding_id: id,
    device_id: deviceId,
    ...overrides,
  });
}

function resetApp() {
  for (const key of Object.keys(storage)) delete storage[key];
  pendingRequests.length = 0;
  app.globalData = {
    identity: { user_id: "person_owner", account_type: "registered" },
    accessToken: "test-token",
    accessTokenExpiresAt: Date.now() + 60_000,
    authEpoch: 0,
  };
  api.clearRuntimeProfileMemory();
}

function takeRequest() {
  const request = pendingRequests.shift();
  assert.ok(request, "缺少设备绑定发现请求");
  assert.match(request.url, /\/v1\/device-bindings$/);
  assert.equal(request.method, "GET");
  return request;
}

function respond(bindings, statusCode = 200) {
  takeRequest().success({ statusCode, data: { bindings } });
}

beforeEach(resetApp);

test("a single server binding is restored on a new client", async () => {
  const serverBinding = manifest("bd_server", "dev_server");
  const syncing = api.syncDeviceBindings();
  respond([serverBinding]);

  const state = await syncing;
  assert.equal(state.status, "ready");
  assert.equal(state.binding.binding_id, "bd_server");
  assert.equal(binding.readBindingManifest().device_id, "dev_server");
});

test("multiple server bindings require an explicit selection", async () => {
  const syncing = api.syncDeviceBindings();
  respond([manifest("bd_a", "dev_a"), manifest("bd_b", "dev_b")]);

  const state = await syncing;
  assert.equal(state.status, "choose");
  assert.equal(state.bindings.length, 2);
  assert.equal(binding.readBindingManifest(), null);
});

test("an existing valid local selection is preserved", async () => {
  const local = manifest("bd_b", "dev_b");
  binding.saveBindingManifest(local);
  const syncing = api.syncDeviceBindings();
  respond([manifest("bd_a", "dev_a"), local]);

  const state = await syncing;
  assert.equal(state.status, "ready");
  assert.equal(state.binding.binding_id, "bd_b");
});

test("a removed cached binding is cleared when the account needs a new selection", async () => {
  binding.saveBindingManifest(manifest("bd_removed", "dev_removed"));
  const syncing = api.syncDeviceBindings();
  respond([manifest("bd_a", "dev_a"), manifest("bd_b", "dev_b")]);
  assert.equal((await syncing).status, "choose");
  assert.equal(binding.readBindingManifest(), null);
});

test("an old account 401 cannot clear the new account session or device", async () => {
  const syncing = api.syncDeviceBindings();
  const oldRequest = takeRequest();
  app.globalData.authEpoch += 1;
  app.globalData.identity = { user_id: "person_new", account_type: "registered" };
  app.globalData.accessToken = "new-token";
  api.selectDeviceBinding(manifest("bd_new", "dev_new"));
  oldRequest.success({ statusCode: 401, data: { detail: "expired" } });
  await syncing;
  assert.equal(app.globalData.identity.user_id, "person_new");
  assert.equal(app.globalData.accessToken, "new-token");
  assert.equal(binding.readBindingManifest().device_id, "dev_new");
});

test("a newer active binding for the same device replaces the cached version", async () => {
  binding.saveBindingManifest(manifest("bd_old", "dev_same"));
  const next = manifest("bd_new", "dev_same", {
    binding_version: 2,
    reason: "supersede",
    supersedes_binding_id: "bd_old",
  });
  const syncing = api.syncDeviceBindings();
  respond([next]);

  const state = await syncing;
  assert.equal(state.binding.binding_id, "bd_new");
  assert.equal(binding.readBindingManifest().binding_version, 2);
});

test("only an explicit empty server list clears local device context", async () => {
  const local = manifest("bd_local", "dev_local");
  binding.saveBindingManifest(local);
  storage[RUNTIME_PROFILE_KEY] = { profile: { runtime_profile_id: "old" } };
  const syncing = api.syncDeviceBindings();
  respond([]);

  const state = await syncing;
  assert.equal(state.status, "empty");
  assert.equal(binding.readBindingManifest(), null);
  assert.equal(storage[RUNTIME_PROFILE_KEY], undefined);
});

test("a network failure retains the current valid cached binding", async () => {
  const local = manifest("bd_cached", "dev_cached");
  binding.saveBindingManifest(local);
  const syncing = api.syncDeviceBindings();
  takeRequest().fail({ errMsg: "request:fail timeout" });

  const state = await syncing;
  assert.equal(state.status, "cached");
  assert.equal(state.binding.binding_id, "bd_cached");
  assert.equal(binding.readBindingManifest().binding_id, "bd_cached");
});

test("a network failure without a cache returns an error state", async () => {
  const syncing = api.syncDeviceBindings();
  takeRequest().fail({ errMsg: "request:fail timeout" });

  const state = await syncing;
  assert.equal(state.status, "error");
  assert.equal(state.binding, null);
});

test("binding discovery rejects non-canonical, inactive, and duplicate results", async (t) => {
  const cases = [
    { name: "non-canonical", bindings: [{ binding_id: "partial" }] },
    {
      name: "inactive",
      bindings: [manifest("bd_revoked", "dev_revoked", { status: "revoked", reason: "unbind" })],
    },
    {
      name: "duplicate binding id",
      bindings: [manifest("bd_dup", "dev_a"), manifest("bd_dup", "dev_b")],
    },
    {
      name: "duplicate active device",
      bindings: [manifest("bd_a", "dev_dup"), manifest("bd_b", "dev_dup")],
    },
  ];

  for (const item of cases) {
    await t.test(item.name, async () => {
      const syncing = api.syncDeviceBindings();
      respond(item.bindings);
      const state = await syncing;
      assert.equal(state.status, "error");
      assert.equal(state.error.code, "invalid_device_bindings");
      assert.equal(binding.readBindingManifest(), null);
    });
  }
});

test("a late discovery response cannot overwrite a newer device selection", async () => {
  const oldBinding = manifest("bd_old", "dev_old");
  const newBinding = manifest("bd_new", "dev_new");
  binding.saveBindingManifest(oldBinding);
  const syncing = api.syncDeviceBindings();

  api.selectDeviceBinding(newBinding);
  respond([oldBinding]);
  const state = await syncing;

  assert.equal(state.binding.binding_id, "bd_new");
  assert.equal(binding.readBindingManifest().binding_id, "bd_new");
});

test("a late discovery response cannot restore a binding after logout", async () => {
  const oldBinding = manifest("bd_old", "dev_old");
  binding.saveBindingManifest(oldBinding);
  const syncing = api.syncDeviceBindings();

  api.logoutLocal();
  respond([oldBinding]);
  const state = await syncing;

  assert.equal(state.binding, null);
  assert.equal(binding.readBindingManifest(), null);
});
