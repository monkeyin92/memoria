const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
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

test("device is the first tab and home voice page is removed", () => {
  assert.equal(appConfig.pages[0], "pages/device/index");
  assert.equal(appConfig.pages.includes("pages/home/index"), false);
  assert.ok(appConfig.pages.includes("pages/device-onboarding/index"));
  assert.deepEqual(
    appConfig.tabBar?.list?.map((item) => [item.pagePath, item.text]),
    [
      ["pages/device/index", "设备"],
      ["pages/memory/index", "回顾"],
      ["pages/profile/index", "我的"],
    ],
  );
});
