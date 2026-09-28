const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const root = path.join(__dirname, "..");
const appConfig = JSON.parse(fs.readFileSync(path.join(root, "app.json"), "utf8"));
const { TAB_ROUTES, isTabRoute } = require("../utils/tab-routes");

const tabRoutes = appConfig.tabBar.list.map((tab) => `/${tab.pagePath}`);
const pageRoutes = new Set(appConfig.pages.map((page) => `/${page}`));
const SKIP_DIRS = new Set(["node_modules", "tests"]);

function sourceFiles(extension, dir = root) {
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) files.push(...sourceFiles(extension, path.join(dir, entry.name)));
    } else if (entry.name.endsWith(extension)) {
      files.push(path.join(dir, entry.name));
    }
  }
  return files;
}

// 返回 `wx.<api>(` 之后到配对右括号为止的参数文本，覆盖多行与三元表达式的 url。
function callArguments(source, start) {
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    if (source[index] === "(") depth += 1;
    else if (source[index] === ")") {
      depth -= 1;
      if (depth === 0) return source.slice(start + 1, index);
    }
  }
  return source.slice(start);
}

test("tab route helper mirrors app.json tabBar exactly", () => {
  assert.deepEqual([...TAB_ROUTES].sort(), [...tabRoutes].sort());
  for (const route of tabRoutes) {
    assert.equal(isTabRoute(route), true, route);
    assert.equal(isTabRoute(`${route}?from=test`), true, route);
  }
  assert.equal(isTabRoute("/pages/auth/index"), false);
  assert.equal(isTabRoute(undefined), false);
});

test("navigateTo / redirectTo never target a tabBar page", () => {
  const offenders = [];
  let checked = 0;
  for (const file of sourceFiles(".js")) {
    const source = fs.readFileSync(file, "utf8");
    for (const match of source.matchAll(/\bwx\.(navigateTo|redirectTo)\s*\(/g)) {
      const args = callArguments(source, match.index + match[0].length - 1);
      for (const [route] of args.matchAll(/\/pages\/[\w-]+\/index/g)) {
        checked += 1;
        const where = `${path.relative(root, file)}: wx.${match[1]} -> ${route}`;
        if (isTabRoute(route)) offenders.push(`${where} (tab page, use wx.switchTab)`);
        else if (!pageRoutes.has(route)) offenders.push(`${where} (not declared in app.json pages)`);
      }
    }
  }
  assert.ok(checked > 0, "scanner found no navigation targets; check the pattern");
  assert.deepEqual(offenders, []);
});

test("<navigator> links to tabBar pages declare open-type switchTab", () => {
  const offenders = [];
  for (const file of sourceFiles(".wxml")) {
    const source = fs.readFileSync(file, "utf8");
    for (const [tag] of source.matchAll(/<navigator\b[^>]*>/g)) {
      const url = tag.match(/\burl="([^"]*)"/)?.[1];
      if (!url || !isTabRoute(url)) continue;
      const openType = tag.match(/\bopen-type="([^"]*)"/)?.[1] || "navigate";
      if (openType !== "switchTab" && openType !== "reLaunch") {
        offenders.push(`${path.relative(root, file)}: ${tag}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});

test("login returns to every tab page with switchTab and to other pages with redirectTo", () => {
  const apiPath = require.resolve("../utils/api");
  const authPath = require.resolve("../pages/auth/index");
  const previous = { wx: global.wx, Page: global.Page };
  const calls = [];
  let definition = null;

  require.cache[apiPath] = { exports: { hasAuthenticatedSession: () => true } };
  global.wx = {
    switchTab: ({ url }) => calls.push(["switchTab", url]),
    redirectTo: ({ url }) => calls.push(["redirectTo", url]),
    navigateTo: ({ url }) => calls.push(["navigateTo", url]),
  };
  global.Page = (value) => {
    definition = value;
  };
  delete require.cache[authPath];
  require(authPath);

  function finish(redirect) {
    calls.length = 0;
    definition.finishLogin.call({ data: { redirect } });
    return calls.slice();
  }

  try {
    for (const route of tabRoutes) {
      assert.deepEqual(finish(`${route}?from=login`), [["switchTab", route]], route);
    }
    assert.deepEqual(finish("/pages/bind/index?claim_id=c1"), [
      ["redirectTo", "/pages/bind/index?claim_id=c1"],
    ]);
    assert.deepEqual(finish(""), [["switchTab", "/pages/home/index"]]);
  } finally {
    delete require.cache[authPath];
    delete require.cache[apiPath];
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete global[key];
      else global[key] = value;
    }
  }
});
