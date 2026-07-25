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
