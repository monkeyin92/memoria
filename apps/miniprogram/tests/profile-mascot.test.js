const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const template = fs.readFileSync(path.join(root, "pages/profile/index.wxml"), "utf8");
const styles = fs.readFileSync(path.join(root, "pages/profile/index.wxss"), "utf8");

test("profile hero uses the WeChat avatar or a name initial, not a robot mascot", () => {
  assert.match(template, /wx:if="\{\{profile\.avatar_url\}\}"[\s\S]*class="user-avatar"/);
  assert.match(template, /class="hero-initial"/);
  assert.match(template, /class="hero-initial-text"\>\{\{profileInitial\}\}/);
  assert.doesNotMatch(template, /profile-mascot-shell|profile-face|hero-avatar"/);
  assert.doesNotMatch(styles, /\.profile-face\s*\{/);
  assert.match(styles, /\.user-avatar\s*\{[^}]*border-radius:\s*50%/s);
});

test("profile companion picker is persona and voice text, not robot artwork", () => {
  assert.match(template, /人格与声音/);
  assert.match(template, /对话只在机器人上进行/);
  assert.match(template, /下次唤醒才会换成对应声音/);
  assert.match(template, /声音 · \{\{item\.voiceName\}\}/);
  assert.match(template, /自定义人格与声音/);
  assert.doesNotMatch(template, /陪伴方式/);
  assert.doesNotMatch(template, /companion-mascot-shell|companion-face|companion-image/);
  assert.doesNotMatch(template, /assets\/companions/);
  assert.match(template, /class="persona-row \{\{!customPersonaActive && profile\.companion_id === item\.id/);
  assert.match(styles, /\.persona-row\s*\{[^}]*min-height:\s*96rpx/s);
  assert.match(styles, /\.persona-selected\s*\{/);
});
