const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const projectConfig = JSON.parse(
  fs.readFileSync(path.join(root, "project.config.example.json"), "utf8"),
);
const homeTemplate = fs.readFileSync(
  path.join(root, "pages/home/index.wxml"),
  "utf8",
);
const homeScript = fs.readFileSync(
  path.join(root, "pages/home/index.js"),
  "utf8",
);

test("dynamic companion images are explicitly retained in the Mini Program package", () => {
  assert.match(
    homeTemplate,
    /\/assets\/companions\/alpha\/\{\{companion\.id\}\}\.webp/,
  );
  assert.match(
    homeScript,
    /\/assets\/companions\/alpha\/\$\{companion\.id\}\.webp/,
  );
  assert.ok(
    projectConfig.packOptions.include.some(
      (entry) =>
        entry.type === "folder" && entry.value === "assets/companions/alpha",
    ),
  );
});
