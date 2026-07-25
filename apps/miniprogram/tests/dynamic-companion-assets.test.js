const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const homeTemplate = fs.readFileSync(
  path.join(root, "pages/home/index.wxml"),
  "utf8",
);
const homeScript = fs.readFileSync(
  path.join(root, "pages/home/index.js"),
  "utf8",
);
const profileTemplate = fs.readFileSync(
  path.join(root, "pages/profile/index.wxml"),
  "utf8",
);
const profileScript = fs.readFileSync(
  path.join(root, "pages/profile/index.js"),
  "utf8",
);

const companionIds = ["starlight", "taoxi", "mianmian", "axu", "xuanmo"];

test("companion images use literal source paths so real packages retain them", () => {
  for (const companionId of companionIds) {
    const assetPath = `/assets/companions/alpha/${companionId}.webp`;
    assert.match(homeTemplate, new RegExp(`src="${assetPath}"`));
    assert.match(profileTemplate, new RegExp(`src="${assetPath}"`));
    assert.ok(
      fs.statSync(path.join(root, assetPath)).size > 0,
      `${assetPath} must exist and be non-empty`,
    );
  }
  assert.doesNotMatch(
    homeTemplate,
    /\/assets\/companions\/alpha\/\{\{companion\.id\}\}\.webp/,
  );
  assert.doesNotMatch(
    profileTemplate,
    /\/assets\/companions\/alpha\/\{\{item\.id\}\}\.webp/,
  );
  assert.doesNotMatch(homeScript, /\/assets\/companions\/alpha\/\$\{companion\.id\}\.webp/);
  assert.doesNotMatch(profileScript, /\/assets\/companions\/alpha\/\$\{.+?\}\.webp/);
});
