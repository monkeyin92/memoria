const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");

test("miniprogram has no voice-clone gate or profile entry", () => {
  assert.equal(fs.existsSync(path.join(root, "utils/voice-clone-gate.js")), false);
  const profile = fs.readFileSync(path.join(root, "pages/profile/index.js"), "utf8");
  const wxml = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
  assert.equal(profile.includes("voice-clone-gate"), false);
  assert.equal(profile.includes("voiceCloneOpen"), false);
  assert.equal(profile.includes("isVoiceCloneOpen"), false);
  assert.equal(wxml.includes("声音复刻"), false);
  assert.equal(wxml.includes("voiceCloneOpen"), false);
});
