const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const template = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
const styles = fs.readFileSync(path.join(root, "pages/profile/index.wxss"), "utf8");

test("profile hero keeps a visible default mascot face overlay", () => {
  assert.match(template, /class="profile-face"/);
  assert.match(template, /class="profile-face-eye profile-face-eye-l"/);
  assert.match(template, /class="profile-face-mouth"/);
  assert.match(styles, /\.profile-face\s*\{[^}]*position:\s*absolute/s);
  assert.match(styles, /\.profile-face-eye\s*\{/);
  assert.match(styles, /\.profile-face-mouth\s*\{/);
});
