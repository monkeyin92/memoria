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

test("auth introductory copy puts each text segment on its own line", () => {
  assert.match(
    authTemplate,
    /<view class="auth-copy">[\s\S]*?<text class="eyebrow auth-copy-line">[\s\S]*?<text class="page-title auth-copy-line">[\s\S]*?<text class="page-subtitle auth-copy-line">/,
  );
  assert.match(
    authStyles,
    /\.auth-copy-line\s*\{[\s\S]*?display:\s*block\s*;/,
  );
});

test("auth mascot has no negative bottom margin that crowds the copy", () => {
  assert.doesNotMatch(
    authStyles,
    /\.brand-mascot\s*\{[\s\S]*?margin:\s*0\s+auto\s+-\d+rpx\s*;/,
  );
});
