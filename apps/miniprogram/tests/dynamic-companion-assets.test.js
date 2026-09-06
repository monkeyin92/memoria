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
const { companions } = require("../utils/companions");

test("Mini Program pages do not pack or render companion robot artwork", () => {
  assert.equal(
    (projectConfig.packOptions.include || []).some(
      (entry) => String(entry.value).includes("assets/companions"),
    ),
    false,
  );
  for (const companion of companions) {
    assert.equal(companion.image, undefined);
  }
  for (const template of pageTemplates) {
    assert.doesNotMatch(
      template.source,
      /\/assets\/companions\//,
      `${template.name} must not reference companion robot images`,
    );
    assert.doesNotMatch(
      template.source,
      /src="\/[^"]+\.(webp|jpg|jpeg|png)"/,
      `${template.name} must not use local companion artwork`,
    );
  }
});
