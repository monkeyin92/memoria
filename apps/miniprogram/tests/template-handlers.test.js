const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { ROOT, createWx, loadPage } = require("../scripts/mp-harness/runtime");

function templateHandlers(dir) {
  const source = fs.readFileSync(path.join(ROOT, dir, "index.wxml"), "utf8");
  const names = new Set();
  for (const match of source.matchAll(/\s(?:capture-)?(?:bind|catch|mut-bind):?[a-z]+="([^"]*)"/g)) {
    const value = match[1];
    if (!value.includes("{{")) {
      if (value) names.add(value);
      continue;
    }
    for (const quoted of value.matchAll(/'([A-Za-z_]\w*)'/g)) names.add(quoted[1]);
  }
  return [...names].sort();
}

function componentMethods(dir) {
  let definition = null;
  const previous = { wx: global.wx, Component: global.Component };
  global.wx = createWx().wx;
  global.Component = (def) => {
    definition = def;
  };
  try {
    const file = path.join(ROOT, dir, "index.js");
    delete require.cache[require.resolve(file)];
    require(file);
  } finally {
    global.wx = previous.wx;
    global.Component = previous.Component;
  }
  return definition?.methods || {};
}

function missingHandlers(kind) {
  const missing = [];
  for (const name of fs.readdirSync(path.join(ROOT, kind))) {
    const dir = `${kind}/${name}`;
    if (!fs.existsSync(path.join(ROOT, dir, "index.wxml"))) continue;
    const handlers = templateHandlers(dir);
    if (kind === "pages") {
      const harness = loadPage(`${dir}/index`);
      try {
        for (const handler of handlers) {
          if (typeof harness.page[handler] !== "function") missing.push(`${dir}: ${handler}`);
        }
      } finally {
        harness.restore();
      }
    } else {
      const methods = componentMethods(dir);
      for (const handler of handlers) {
        if (typeof methods[handler] !== "function") missing.push(`${dir}: ${handler}`);
      }
    }
  }
  return missing;
}

test("template handler scan reads static and conditional bindings", () => {
  assert.deepEqual(templateHandlers("pages/profile").slice(0, 4), [
    "chooseDeviceBinding",
    "closeDeleteModal",
    "confirmDelete",
    "copyAccountId",
  ]);
  assert.ok(templateHandlers("pages/profile").includes("loginFromProfile"));
  assert.ok(templateHandlers("pages/device").includes("showConnectionHelp"));
});

test("every page template event names a method the page defines", () => {
  assert.deepEqual(missingHandlers("pages"), []);
});

test("every component template event names a method the component defines", () => {
  assert.deepEqual(missingHandlers("components"), []);
});
