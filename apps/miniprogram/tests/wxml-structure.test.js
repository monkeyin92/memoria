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

test("WXML templates never use inline <svg>, which WeChat does not render", () => {
  const root = path.join(__dirname, "..");
  const offenders = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".wxml") && /<svg[\s>]/.test(fs.readFileSync(full, "utf8"))) {
        offenders.push(path.relative(root, full));
      }
    }
  };
  walk(root);
  assert.deepEqual(offenders, []);
});
