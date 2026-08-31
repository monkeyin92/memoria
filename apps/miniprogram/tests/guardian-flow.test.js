const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..");

test("guardian page covers child confirmation, granular consent, summary, and alerts", () => {
  const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
  const script = fs.readFileSync(path.join(root, "pages/guardian/index.js"), "utf8");
  const template = fs.readFileSync(path.join(root, "pages/guardian/index.wxml"), "utf8");

  assert.ok(appConfig.pages.includes("pages/guardian/index"));
  assert.match(script, /createGuardianLink/);
  assert.match(script, /confirmGuardianLink/);
  assert.match(script, /grantGuardianConsent/);
  assert.match(script, /revokeGuardianConsent/);
  assert.match(script, /getGuardianNotifications/);
  assert.match(template, /输入 8 位绑定码/);
  assert.match(script, /语音陪伴/);
  assert.match(script, /学习与成长记录/);
  assert.match(script, /每周成长小结/);
  assert.match(template, /不含对话原文/);
  assert.doesNotMatch(template, /心理监测|心理诊断评分|严重度分级/);
});

test("student notice and bind consent remain explicit client choices", () => {
  const profile = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
  const bind = fs.readFileSync(path.join(root, "pages/bind/index.wxml"), "utf8");
  const api = fs.readFileSync(path.join(root, "utils/api.js"), "utf8");

  // 敏感入口只能由 Runtime Profile capabilities 驱动，WXML 不得按本地年龄显示。
  assert.match(profile, /guardianEntryAllowed/);
  assert.match(profile, /speakerEnrollmentState/);
  assert.match(profile, /digitalSelfEntryAllowed/);
  assert.match(profile, /rawVoiceEntryAllowed/);
  assert.doesNotMatch(profile, /canUseAdultCapabilities|_allowAdultExperience/);
  assert.match(profile, /成长小结与监护授权/);
  assert.match(profile, /敏感能力入口已关闭/);
  assert.match(bind, /英语口语陪练/);
  assert.match(api, /updateDeviceSettings/);
  assert.match(api, /learning_mode/);
  assert.doesNotMatch(api, /session_focus/);
});
