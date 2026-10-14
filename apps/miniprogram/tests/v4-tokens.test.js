const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const RAW_COLOR = /#[0-9a-fA-F]{3,8}\b|rgba?\(/g;
const RAW_FONT_SIZE = /font-size:\s*[\d.]+(?:rpx|px)/g;

function wxssFiles(dir) {
  return fs
    .readdirSync(path.join(root, dir), { recursive: true })
    .filter((file) => file.endsWith(".wxss"))
    .map((file) => path.join(dir, file));
}

test("page and shared stylesheets take every colour from the app.wxss tokens", () => {
  const offenders = [...wxssFiles("pages"), ...wxssFiles("styles")].flatMap((file) =>
    (fs.readFileSync(path.join(root, file), "utf8").match(RAW_COLOR) || []).map((hit) => `${file}: ${hit}`),
  );
  assert.deepEqual(offenders, []);
});

test("type sizes come from the --mp-type scale, so no page drifts to odd or tiny sizes", () => {
  const offenders = ["app.wxss", ...wxssFiles("pages"), ...wxssFiles("styles")].flatMap((file) =>
    (fs.readFileSync(path.join(root, file), "utf8").match(RAW_FONT_SIZE) || []).map((hit) => `${file}: ${hit}`),
  );
  assert.deepEqual(offenders, []);
});

test("app.wxss declares raw colours only inside the page token block", () => {
  const app = fs.readFileSync(path.join(root, "app.wxss"), "utf8");
  const tokenBlock = app.match(/^page \{[\s\S]*?^\}/m)[0];
  assert.ok(tokenBlock.includes("--mp-accent: #4a52c9;"));
  assert.deepEqual(app.replace(tokenBlock, "").match(RAW_COLOR), null);
});
