const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");

process.env.TZ = "Asia/Shanghai";

const { PAGES, VIEWS } = require("../scripts/mp-harness/pages");
const registeredPages = JSON.parse(fs.readFileSync(require.resolve("../app.json"), "utf8")).pages;

function chromeSkipReason() {
  try {
    require.resolve("puppeteer-core");
  } catch {
    return "puppeteer-core is not installed";
  }
  const { chromePath } = require("../scripts/mp-harness/shoot");
  return fs.existsSync(chromePath()) ? "" : `no Chrome at ${chromePath()}`;
}

const skip = chromeSkipReason();

test("the harness covers every app.json page and removes the stale privacy-granted scenario", () => {
  const coveredPages = new Set(PAGES.map((scenario) => scenario.page));
  assert.deepEqual(
    registeredPages.filter((page) => !coveredPages.has(page)),
    [],
  );
  assert.equal(PAGES.some((scenario) => scenario.slug === "privacy-granted"), false);
  assert.ok(PAGES.some((scenario) => scenario.slug === "memory-lifecycle"));
  assert.ok(PAGES.some((scenario) => scenario.slug === "guardian-consent"));
});

test("every page state lays out without overflow, capsule or tab bar collisions at 320/375/390/430 and 1.3x font", { skip: skip || false }, async (t) => {
  const { launch, problems, shoot } = require("../scripts/mp-harness/shoot");
  const browser = await launch();
  const failures = [];
  try {
    for (const scenario of PAGES) {
      for (const view of VIEWS) {
        const { htmlPath, ...result } = await shoot(browser, scenario, view, null);
        const issues = problems(result, view);
        if (issues.length) failures.push(`${scenario.slug}@${view.name}: ${issues.join(" | ")}`);
      }
    }
  } finally {
    await browser.close();
  }
  t.diagnostic(`${PAGES.length * VIEWS.length} layouts checked`);
  assert.deepEqual(failures, []);
});
