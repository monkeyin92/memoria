const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const componentRoot = path.join(__dirname, "..", "components", "device-screen");

test("the device screen exposes its screen face without a camera indicator", () => {
  const template = fs.readFileSync(path.join(componentRoot, "index.wxml"), "utf8");
  const styles = fs.readFileSync(path.join(componentRoot, "index.wxss"), "utf8");

  assert.match(template, /class="ds-screen"/);
  assert.equal((template.match(/class="ds-eye"/g) || []).length, 2);
  assert.match(template, /class="ds-smile"/);
  assert.match(template, /'ds-off'/);
  assert.doesNotMatch(template, /ds-cam/);
  assert.doesNotMatch(styles, /\.ds-cam\b/);
});
