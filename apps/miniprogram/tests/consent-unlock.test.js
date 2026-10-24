const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const legal = require("../utils/legal-release");
const {
  CHILD_INFO_CONSENT_KEY,
  CHILD_INFO_CONSENT_VERSIONS_KEY,
  grantChildInfoConsent,
  recordInternalTestConsentVersions,
} = require("../utils/child-consent");

const GATE = path.join(__dirname, "..", "scripts", "check-release-gate.js");
const ISO_8601 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

function withWx(run) {
  const storage = {};
  const previousWx = global.wx;
  global.wx = {
    getStorageSync: (key) => storage[key],
    setStorageSync: (key, value) => {
      storage[key] = value;
    },
  };
  try {
    run(storage);
  } finally {
    if (previousWx === undefined) delete global.wx;
    else global.wx = previousWx;
  }
}

test("consent stays locked for a pending agreement and a blank effective date", () => {
  assert.equal(legal.INTERNAL_TEST_CONSENT_UNLOCK, false);
  assert.equal(legal.EFFECTIVE_ON, "2026年10月24日");
  assert.equal(legal.consentLocked(true, "2026年4月1日"), true);
  assert.equal(legal.consentLocked(false, ""), true);
  assert.equal(legal.consentLocked(false, "  "), true);
  assert.equal(legal.consentLocked(true, ""), true);
  assert.equal(legal.consentLocked(true, "", true), false);
  assert.equal(legal.consentLocked(false, legal.EFFECTIVE_ON), false);
});

test("a grant outside the unlock path writes accepted and leaves versions absent", () => {
  withWx((storage) => {
    grantChildInfoConsent();
    recordInternalTestConsentVersions(false);
    assert.equal(storage[CHILD_INFO_CONSENT_KEY], "accepted");
    assert.equal(Object.hasOwn(storage, CHILD_INFO_CONSENT_VERSIONS_KEY), false);
  });
  withWx((storage) => {
    storage[CHILD_INFO_CONSENT_KEY] = "accepted";
    grantChildInfoConsent();
    recordInternalTestConsentVersions(false);
    assert.equal(storage[CHILD_INFO_CONSENT_KEY], "accepted");
    assert.equal(Object.hasOwn(storage, CHILD_INFO_CONSENT_VERSIONS_KEY), false);
  });
});

test("an unlock-path grant records versions and keeps the consent string accepted", () => {
  withWx((storage) => {
    storage[CHILD_INFO_CONSENT_KEY] = "accepted";
    const before = Date.now();
    grantChildInfoConsent();
    recordInternalTestConsentVersions(true);
    const after = Date.now();
    assert.equal(storage[CHILD_INFO_CONSENT_KEY], "accepted");
    const versions = storage[CHILD_INFO_CONSENT_VERSIONS_KEY];
    assert.equal(versions.guideVersion, legal.GUIDE_VERSION);
    assert.equal(versions.consentDocVersion, legal.CONSENT_DOC_VERSION);
    assert.equal(versions.guideVersion, "internal-test-guide-20261017-r5");
    assert.equal(versions.consentDocVersion, "internal-test-consent-20261017-r5");
    assert.equal(versions.unlock, "internal-test");
    assert.match(versions.acceptedAt, ISO_8601);
    const acceptedAt = Date.parse(versions.acceptedAt);
    assert.ok(acceptedAt >= before && acceptedAt <= after);
  });
});

function runGate(fixtureSource) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "consent-unlock-"));
  const file = path.join(dir, "legal-release.js");
  fs.writeFileSync(file, fixtureSource);
  try {
    return spawnSync(process.execPath, [GATE, file], { encoding: "utf8" });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test("release gate fails only when the unlock constant is strictly true", () => {
  const opened = runGate("module.exports = { INTERNAL_TEST_CONSENT_UNLOCK: true };\n");
  assert.notEqual(opened.status, 0);
  assert.match(opened.stderr, /INTERNAL_TEST_CONSENT_UNLOCK/);
  assert.match(opened.stderr, /must not commit/);

  const closed = runGate("module.exports = { INTERNAL_TEST_CONSENT_UNLOCK: false };\n");
  assert.equal(closed.status, 0);
});
