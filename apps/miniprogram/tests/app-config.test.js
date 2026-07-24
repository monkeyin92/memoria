const assert = require("node:assert/strict");
const test = require("node:test");

const appConfig = require("../app.json");

const REQUIRED_PRIVATE_INFO_ALLOWLIST = new Set([
  "chooseAddress",
  "chooseLocation",
  "choosePoi",
  "getFuzzyLocation",
  "getLocation",
  "onLocationChange",
  "startLocationUpdate",
  "startLocationUpdateBackground",
]);

test("app config only declares supported required private APIs", () => {
  const invalid = (appConfig.requiredPrivateInfos || []).filter(
    (name) => !REQUIRED_PRIVATE_INFO_ALLOWLIST.has(name),
  );

  assert.deepEqual(invalid, []);
});

test("app config does not declare recorder authorization in the location-only permission map", () => {
  assert.equal(appConfig.permission?.["scope.record"], undefined);
});
