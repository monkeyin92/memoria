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
const faceBadgeTemplate = fs.readFileSync(
  path.join(root, "components/face-badge/index.wxml"),
  "utf8",
);
const { companions } = require("../utils/companions");

// face-badge 的帧列表必须和打包的图片一一对应。
function faceBadgeFrames() {
  const source = fs.readFileSync(path.join(root, "components/face-badge/index.js"), "utf8");
  const match = source.match(/const FRAMES = (\[[^\]]*\]);/);
  return { FRAMES: JSON.parse(match[1].replace(/'/g, '"')) };
}

// 主包 2MB 上限：5 个伙伴 × 6 个表情，每张控制在 30KB 内（合计约 600KB）。
const MASCOT_MAX_BYTES = 30 * 1024;
const MASCOT_MOODS = ["default", "happy", "surprised", "thinking", "dizzy", "sleepy"];

test("every companion ships a compact packed set of expression frames", () => {
  for (const companion of companions) {
    for (const mood of MASCOT_MOODS) {
      const file = path.join(root, "assets/mascots", companion.id, `${mood}.png`);
      assert.ok(fs.existsSync(file), `missing ${mood} mascot for ${companion.id}`);
      assert.ok(
        fs.statSync(file).size <= MASCOT_MAX_BYTES,
        `${companion.id}/${mood} mascot exceeds ${MASCOT_MAX_BYTES} bytes`,
      );
    }
    assert.equal(companion.image, undefined);
  }
  assert.match(faceBadgeTemplate, /src="\/assets\/mascots\/\{\{roleId\}\}\/\{\{item\}\}\.png"/);
  assert.match(faceBadgeTemplate, /src="\/assets\/mascots\/\{\{roleId\}\}\/default\.png"/);
  const { FRAMES } = faceBadgeFrames();
  assert.deepEqual(FRAMES, MASCOT_MOODS);
  // 路径是动态拼接的，ignoreUploadUnusedFiles 看不到引用，必须显式 include。
  assert.ok(
    (projectConfig.packOptions.include || []).some((entry) => entry.value === "assets/mascots"),
  );
});

test("the full expression frame set stays out of the package and out of pages", () => {
  const ignored = (projectConfig.packOptions.ignore || []).map((entry) => String(entry.value));
  assert.ok(ignored.includes("assets/companions"));
  assert.equal(
    (projectConfig.packOptions.include || []).some(
      (entry) => String(entry.value).includes("assets/companions"),
    ),
    false,
  );
  for (const template of pageTemplates) {
    assert.doesNotMatch(
      template.source,
      /\/assets\/(companions|mascots)\//,
      `${template.name} must render companions through <face-badge>`,
    );
  }
});

test("home and device heroes show the selected companion, not the generic robot", () => {
  const home = fs.readFileSync(path.join(root, "pages/home/index.wxml"), "utf8");
  const device = fs.readFileSync(path.join(root, "pages/device/index.wxml"), "utf8");
  assert.match(home, /<face-badge role="\{\{heroRoleId\}\}"[^>]*animated="\{\{true\}\}"[^>]*dim="\{\{!online\}\}"/);
  assert.match(device, /<face-badge role="\{\{heroRoleId\}\}"[^>]*animated="\{\{true\}\}"[^>]*dim="\{\{!online\}\}"/);
  assert.doesNotMatch(home, /<device-screen/);
  assert.doesNotMatch(device, /<device-screen/);
});
