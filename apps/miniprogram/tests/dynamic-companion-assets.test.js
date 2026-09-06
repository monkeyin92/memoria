const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
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
const companions = require("../utils/companions");

test("companion portraits are packed as JPEG and never referenced as local WebP", () => {
  const included = (projectConfig.packOptions.include || []).map((entry) => entry.value);
  for (const companion of companions.companions) {
    assert.match(companion.image, /^\/assets\/companions\/[a-z]+\.jpg$/);
    assert.ok(fs.existsSync(path.join(root, companion.image.slice(1))));
    assert.ok(included.includes(companion.image.slice(1)));
  }
  for (const template of pageTemplates) {
    assert.doesNotMatch(
      template.source,
      /src="\/[^"]+\.webp"/,
      `${template.name} must not use a local WebP image on physical Mini Program WebViews`,
    );
  }
});
