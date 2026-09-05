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

test("app config declares recorder permission only for custom voice samples", () => {
  assert.equal(
    appConfig.permission?.["scope.record"]?.desc,
    "仅用于录制自定义声音样本，供机器人使用；不用于小程序内对话。",
  );
});

test("home is the first tab and stays a status board without in-app chat", () => {
  assert.equal(appConfig.pages[0], "pages/home/index");
  assert.ok(appConfig.pages.includes("pages/home/index"));
  assert.ok(appConfig.pages.includes("pages/device-onboarding/index"));
  assert.deepEqual(
    appConfig.tabBar?.list?.map((item) => [item.pagePath, item.text]),
    [
      ["pages/home/index", "首页"],
      ["pages/device/index", "设备"],
      ["pages/memory/index", "回顾"],
      ["pages/profile/index", "我的"],
    ],
  );
  const deviceTemplate = fs.readFileSync(
    path.join(root, "pages/device/index.wxml"),
    "utf8",
  );
  assert.match(deviceTemplate, /对话请在机器人上完成/);
  assert.doesNotMatch(deviceTemplate, /在线对话|startVoice|startText|sendText/);
});
