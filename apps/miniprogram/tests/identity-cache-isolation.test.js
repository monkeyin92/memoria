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
  const { OnboardingController } = require('../utils/device-onboarding/onboarding-controller');
  const controller = new OnboardingController();
  controller._session = { onboarding_session_id: 'old-session' };
  const resumed = controller.resume('old-session');
  const request = requests.pop();
  login('u_other');
  assert.equal(controller.session, null);
  assert.equal(controller.state, 'prepare');
  request.success({ statusCode: 200, data: {} });
  assert.equal(await resumed, null);
  assert.equal(controller.session, null);
  assertEmpty();
});
