const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const digitalSelfTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/digital-self/index.wxml"),
  "utf8",
);

test("digital-self WXML keeps else branches separate from repeated nodes", () => {
  assert.doesNotMatch(digitalSelfTemplate, /<[^>]*wx:else[^>]*wx:for[^>]*>/);
});
