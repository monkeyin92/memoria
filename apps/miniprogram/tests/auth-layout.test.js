const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const authTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxml"),
  "utf8",
);
const authStyles = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxss"),
  "utf8",
);

test("auth header keeps the brand cue and title on separate lines", () => {
  assert.match(
    authTemplate,
    /<view class="auth-copy">[\s\S]*?<text class="eyebrow auth-copy-line">[\s\S]*?<text class="page-title auth-copy-line">/,
  );
  assert.match(
    authStyles,
    /\.auth-copy-line\s*\{[\s\S]*?display:\s*block\s*;/,
  );
});

test("auth header has no unselected mascot placeholder or redundant subtitle", () => {
  assert.doesNotMatch(authTemplate, /brand-stage|brand-mascot|page-subtitle/);
  assert.doesNotMatch(authStyles, /\.brand-(?:stage|halo|ring|spark|mascot)\b/);
});

test("first registration asks how to address the user and explains the effect", () => {
  assert.match(
    authTemplate,
    /怎么称呼你？\{\{needsSalutation \? ' \*' : ''\}\}[\s\S]*placeholder="例如：朋友、主人、小明"/,
  );
  assert.match(authTemplate, /首次注册后，我会在自然的对话里这样称呼你。/);
});
