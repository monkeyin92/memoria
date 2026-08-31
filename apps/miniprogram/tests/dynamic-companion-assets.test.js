const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const profileTemplate = fs.readFileSync(
  path.join(root, "pages/profile/index.wxml"),
  "utf8",
);
const profileScript = fs.readFileSync(
  path.join(root, "pages/profile/index.js"),
  "utf8",
);
const pageTemplates = fs
  .readdirSync(path.join(root, "pages"), { recursive: true })
  .filter((name) => name.endsWith(".wxml"))
  .map((name) => ({
    name,
    source: fs.readFileSync(path.join(root, "pages", name), "utf8"),
  }));
const projectConfig = JSON.parse(
  fs.readFileSync(path.join(root, "project.config.example.json"), "utf8"),
);

const companionIds = ["starlight", "taoxi", "mianmian", "axu", "xuanmo"];

test("companion images use literal source paths so real packages retain them", () => {
  assert.ok(
    projectConfig.packOptions.include.some(
      (entry) =>
        entry.type === "folder" && entry.value === "assets/companions/miniprogram",
    ),
  );
  for (const companionId of companionIds) {
    const assetPath = `/assets/companions/miniprogram/${companionId}.png`;
    assert.match(profileTemplate, new RegExp(`src="${assetPath}"`));
    const asset = fs.readFileSync(path.join(root, assetPath));
    assert.deepEqual(
      [...asset.subarray(0, 8)],
      [137, 80, 78, 71, 13, 10, 26, 10],
      `${assetPath} must be a local PNG supported by physical Mini Program WebViews`,
    );
  }
  assert.doesNotMatch(
    profileTemplate,
    /\/assets\/companions\/miniprogram\/\{\{item\.id\}\}\.png/,
  );
  assert.doesNotMatch(profileScript, /\/assets\/companions\/miniprogram\/\$\{.+?\}\.png/);
  assert.doesNotMatch(profileTemplate, /src="\/[^"]+\.webp"/);
  for (const template of pageTemplates) {
    assert.doesNotMatch(
      template.source,
      /src="\/[^"]+\.webp"/,
      `${template.name} must not use a local WebP image on physical Mini Program WebViews`,
    );
  }
});
