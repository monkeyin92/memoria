const assert = require('node:assert/strict');
const { test, beforeEach } = require('node:test');
const { canonicalManifest } = require('./manifest-fixtures');
const cache = new Map();
const requests = [];
let app;
global.App = (definition) => { app = definition; };
global.getApp = () => app;
global.wx = {
  getStorageSync: (key) => cache.get(key),
  setStorageSync: (key, value) => cache.set(key, value),
  removeStorageSync: (key) => cache.delete(key),
  request: (request) => requests.push(request),
};
const api = require('../utils/api');
const binding = require('../utils/device-binding');
const labels = require('../utils/subject-label');
const consent = require('../utils/child-consent');
const onboarding = require('../utils/device-onboarding/session-store');
const { OnboardingController } = require('../utils/device-onboarding/onboarding-controller');
function session(id = 'old-session') {
  return {
    onboarding_session_id: id, state: 'qr_verified', state_version: 1, activation_version: 0,
    expires_at: '2099-01-01T00:00:00Z',
    device: { device_id: 'dev_01', display_tail: 'ABCD', model: 'memoria',
      firmware_version: '0.1.0', claim_status: 'unclaimed' },
    provisioning: { transport: 'ble', ble_name: 'MEM-ABCD',
      service_uuid: '12345678-1234-4234-8234-1234567890ab', protocol_version: 1 },
  };
}
function login(id, account_type = 'registered') {
  app.setAuthenticatedIdentity({ user_id: id, account_type, access_token: `token-${id}`, expires_in: 3600 });
}
function seed() {
  binding.saveBindingManifest(canonicalManifest({ account_owner_id: 'u_parent' }));
  cache.set('memoria:miniprogram:device:runtime-profile', { profile: { session_id: 'old' } });
  labels.saveSubjectLabel({ bindingId: 'old', deviceId: 'old', label: 'parent child' });
  consent.grantChildInfoConsent();
  consent.recordInternalTestConsentVersions(true);
  onboarding.saveOnboardingSessionId('old-session');
}
function assertEmpty() {
  assert.equal(binding.readBindingManifest(), null);
  assert.equal(cache.has('memoria:miniprogram:device:runtime-profile'), false);
  assert.equal(cache.has(labels.STORAGE_KEY), false);
  assert.equal(consent.hasChildInfoConsent(), false);
  assert.equal(cache.has(consent.CHILD_INFO_CONSENT_VERSIONS_KEY), false);
  assert.equal(onboarding.readOnboardingSessionId(), '');
}
beforeEach(() => {
  cache.clear(); requests.length = 0;
  delete require.cache[require.resolve('../app')];
  require('../app');
});
test('real app switch followed by real API network failure cannot return parent binding', async () => {
  login('u_parent'); seed();
  let notified = 0;
  app.subscribeAuthCleared(() => { assertEmpty(); notified++; });
  login('u_other');
  const pending = api.syncDeviceBindings();
  requests.pop().fail({ errMsg: 'offline' });
  const result = await pending;
  assert.equal(result.binding, null);
  assert.equal(result.status, 'error');
  assertEmpty(); assert.equal(notified, 1);
});
test('same owner token refresh preserves offline cache and reading receipt', async () => {
  login('u_parent'); seed(); login('u_parent');
  const pending = api.syncDeviceBindings(); requests.pop().fail({ errMsg: 'offline' });
  assert.equal((await pending).binding.account_owner_id, 'u_parent');
  assert.equal(consent.hasChildInfoConsent(), true);
  assert.equal(onboarding.readOnboardingSessionId(), 'old-session');
});
for (const exit of ['direct-clear', 'logout', '401', 'guest-promotion']) {
  test(`${exit} clears every owned cache before subsequent login`, async () => {
    login('u_parent', exit === 'guest-promotion' ? 'anonymous' : 'registered'); seed();
    if (exit === 'direct-clear') app.clearAuthenticatedIdentity();
    if (exit === 'logout') api.logoutLocal();
    if (exit === 'guest-promotion') login('u_parent');
    if (exit === '401') {
      const pending = api.syncDeviceBindings();
      requests.pop().success({ statusCode: 401, data: {} }); await pending;
    }
    assertEmpty(); login('u_other'); assertEmpty();
  });
}
test('late discovery and onboarding responses cannot refill a switched identity', async () => {
  login('u_parent'); seed();
  const discovery = api.syncDeviceBindings();
  const oldDiscovery = requests.pop();
  const session = api.getOnboardingSession('old-session');
  const rejected = assert.rejects(session, /身份|登录/);
  const oldSession = requests.pop();
  login('u_other');
  oldDiscovery.success({ statusCode: 200, data: { bindings: [canonicalManifest()] } });
  oldSession.success({ statusCode: 200, data: {} });
  await discovery; await rejected; assertEmpty();
});
test('onboarding controller synchronously drops owned state and fences a late resume', async () => {
  login('u_parent');
  const snapshots = [];
  const controller = new OnboardingController({ onChange: (value) => snapshots.push(value) });
  controller._session = { onboarding_session_id: 'old-session' };
  const resumed = controller.resume('old-session');
  const request = requests.pop();
  login('u_other');
  assert.equal(controller.session, null);
  assert.equal(controller.state, 'prepare');
  request.success({ statusCode: 200, data: session() });
  assert.equal(await resumed, null);
  assert.equal(controller.session, null);
  controller.startScan();
  assert.equal(snapshots.at(-1).state, 'scan');
  assertEmpty();
  controller.dispose();
});

for (const entry of ['401-same-owner', 'guest-promotion', 'first-login']) {
  test(`${entry} keeps the mounted controller emitting and subscribed`, async () => {
    if (entry !== 'first-login') login('u_parent', entry === 'guest-promotion' ? 'anonymous' : 'registered');
    const snapshots = [];
    const controller = new OnboardingController({ onChange: (value) => snapshots.push(value) });
    try {
      if (entry === '401-same-owner') {
        const pending = api.syncDeviceBindings();
        requests.pop().success({ statusCode: 401, data: {} });
        await pending;
      }
      login('u_parent');
      controller.startScan();
      assert.equal(snapshots.at(-1).state, 'scan');
      const pending = controller.resume('new-session');
      requests.pop().success({ statusCode: 200, data: session('new-session') });
      assert.equal((await pending).onboarding_session_id, 'new-session');
      assert.equal(snapshots.at(-1).session.onboarding_session_id, 'new-session');
      app.clearAuthenticatedIdentity();
      assert.equal(snapshots.at(-1).state, 'prepare');
      assert.equal(snapshots.at(-1).session, null);
      assert.equal(snapshots.at(-1).busy, false);
    } finally { controller.dispose(); }
    const count = snapshots.length;
    app.clearAuthenticatedIdentity();
    controller.startScan();
    assert.equal(snapshots.length, count);
  });
}

test('identity reset clears BLE, secrets and progress to the cold controller snapshot', () => {
  login('u_parent');
  const snapshots = [];
  const controller = new OnboardingController({ onChange: (value) => snapshots.push(value) });
  const cold = snapshots[0];
  const disposed = [];
  try {
    controller._session = session();
    controller._claim = { device_id: 'dev_01' };
    controller._binding = { device_id: 'dev_01' };
    controller._activation = { status: 'pending_manifest' };
    controller._wifiNetworks = [{ ssid: 'private' }];
    controller._network = { ssid: 'private', phase: 'joining' };
    controller._bootstrapPop = 'private-pop';
    controller._state = 'activation';
    controller._busy = true;
    controller._error = 'old error';
    controller._errorCode = 'OLD_ERROR';
    controller._activationSince = Date.now();
    controller.setWifiCredentials({ ssid: 'private', password: 'private-password' });
    const bytes = controller._wifiPassword._bytes;
    controller._adapter = { dispose: () => disposed.push('adapter') };
    controller._transport = { dispose: () => disposed.push('transport') };
    controller.startActivationPolling();
    app.clearAuthenticatedIdentity();
    assert.deepEqual(snapshots.at(-1), cold);
    assert.deepEqual(disposed, ['transport', 'adapter']);
    assert.equal(controller._wifiPassword.length, 0);
    assert.equal(bytes.every((byte) => byte === 0), true);
    assert.equal(controller._wifiSsid, '');
    assert.equal(controller._bootstrapPop, '');
    assert.equal(controller._activationTimer, null);
  } finally { controller.dispose(); }
});

test('same owner cold-start token refresh preserves the mounted onboarding snapshot', async () => {
  login('u_parent'); seed();
  delete require.cache[require.resolve('../app')];
  require('../app'); app.onLaunch();
  const snapshots = [];
  const controller = new OnboardingController({ onChange: (value) => snapshots.push(value) });
  try {
    const pending = controller.resume('old-session');
    requests.pop().success({ statusCode: 200, data: session() });
    assert.equal((await pending).onboarding_session_id, 'old-session');
    const before = controller.snapshot();
    const count = snapshots.length;
    login('u_parent');
    assert.deepEqual(controller.snapshot(), before);
    assert.equal(snapshots.length, count);
    assert.equal(consent.hasChildInfoConsent(), true);
    assert.equal(binding.readBindingManifest().account_owner_id, 'u_parent');
  } finally { controller.dispose(); }
});

for (const result of ['networks', 'error']) {
  test(`late BLE scan ${result} cannot refill a controller after identity reset`, async () => {
    login('u_parent');
    const snapshots = [];
    const controller = new OnboardingController({ onChange: (value) => snapshots.push(value) });
    let resolveScan;
    let rejectScan;
    const response = new Promise((resolve, reject) => { resolveScan = resolve; rejectScan = reject; });
    controller._transport = { request: () => response, dispose() {} };
    try {
      const scanning = controller.loadWifiNetworks();
      app.clearAuthenticatedIdentity(); login('u_parent'); controller.startScan();
      const count = snapshots.length;
      const current = controller.snapshot();
      if (result === 'networks') resolveScan({ networks: [{ ssid: 'previous-owner-wifi', rssi: -40, security: 'wpa2', channel: 6 }] });
      else rejectScan(new Error('previous-owner-error'));
      assert.deepEqual(await scanning, []);
      assert.deepEqual(controller.snapshot(), current);
      assert.equal(snapshots.length, count);
    } finally { controller.dispose(); }
  });
}
