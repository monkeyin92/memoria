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

test("lengths are px so the 390 pt design frame renders 1:1, never rpx", () => {
  const templates = ["pages", "components", "custom-tab-bar"].flatMap((dir) =>
    fs
      .readdirSync(path.join(root, dir), { recursive: true })
      .filter((file) => /\.(wxss|wxml)$/.test(file))
      .map((file) => path.join(dir, file)),
  );
  const offenders = ["app.wxss", ...templates, ...wxssFiles("styles")].flatMap((file) =>
    (fs.readFileSync(path.join(root, file), "utf8").match(/[\d}]rpx/g) || []).map((hit) => `${file}: ${hit}`),
  );
  assert.deepEqual(offenders, []);
});

test("the rpx-to-px codemod halves literal and bound lengths", () => {
  const { halve } = require("../scripts/codemods/rpx-to-px");
  assert.equal(
    halve('padding: 32rpx 1rpx; margin: -1.2rpx; style="width: {{size}}rpx; height: {{size * 0.94}}rpx"'),
    'padding: 16px 0.5px; margin: -0.6px; style="width: {{(size) / 2}}px; height: {{(size * 0.94) / 2}}px"',
  );
});

test("mp-icon tiles of 48px and up take the design's 16px inset radius", () => {
  let definition;
  const previous = global.Component;
  global.Component = (config) => {
    definition = config;
  };
  try {
    delete require.cache[require.resolve("../components/mp-icon/index")];
    require("../components/mp-icon/index");
  } finally {
    global.Component = previous;
  }
  const classesAt = (size) => {
    const instance = { setData(patch) { Object.assign(this, patch); } };
    definition.observers["name, tone, size"].call(instance, "heart", "accent", size);
    return instance.toneClass;
  };
  assert.equal(classesAt(80), "mi-accent");
  assert.equal(classesAt(96), "mi-accent mi-lg");
  assert.equal(classesAt(112), "mi-accent mi-lg");
});

test("app.wxss declares raw colours only inside the page token block", () => {
  const app = fs.readFileSync(path.join(root, "app.wxss"), "utf8");
  const tokenBlock = app.match(/^page \{[\s\S]*?^\}/m)[0];
  assert.ok(tokenBlock.includes("--mp-accent: #4a52c9;"));
  assert.deepEqual(app.replace(tokenBlock, "").match(RAW_COLOR), null);
});
