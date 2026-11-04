const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { launch } = require("./mp-harness/shoot");

const source = path.resolve(process.argv[2] || path.join(__dirname, "../../../docs/design/robot-ui-20261104/preview.html"));
const checks = [];
const errors = [];
const slugs = ["home", "memory", "device", "profile"];

async function main() {
  const browser = await launch();
  try {
    const page = await browser.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewport({ width: 1100, height: 940 });
    await page.goto(pathToFileURL(source).href, { waitUntil: "load" });
    const frame = () => page.frames().find((entry) => entry.parentFrame() === page.mainFrame());
    async function checkScreen(slug, selector) {
      await page.waitForFunction((selected) => screen === selected, {}, slug);
      await frame().waitForSelector(selector);
      const broken = await frame().evaluate(() => [...document.images].filter((image) => !image.complete || !image.naturalWidth).map((image) => image.src));
      assert.deepEqual(broken, []);
    }
    for (const slug of slugs) {
      await page.click(`[data-screen="${slug}"]`);
      await checkScreen(slug, `.mp-page .${slug === "home" ? "home-v4" : `${slug}-page`}`);
      checks.push(`side navigation ${slug}`);
    }
    for (let index = 0; index < slugs.length; index += 1) {
      await frame().click(`[data-handler="onTapTab"][data-index="${index}"]`);
      await checkScreen(slugs[index], ".wx-tabbar-host");
      checks.push(`bottom navigation ${slugs[index]}`);
    }
    await page.click('[data-screen="device"]');
    await checkScreen("device", '[data-handler="toggleManagement"]');
    for (const handler of ["toggleUsage", "toggleManagement", "toggleStories", "toggleMemberSettings", "toggleDiag"]) {
      await frame().click(`[data-handler="${handler}"]`);
      await frame().waitForSelector(handler === "toggleDiag" ? ".diagnostic" : `[data-handler="${handler}"] .row-chev-open`, { timeout: 5000 });
      checks.push(`expanded ${handler}`);
    }
    await frame().click('[data-handler="toggleManagement"]');
    await frame().waitForFunction(() => !document.querySelector('[data-handler="toggleStories"]'));
    checks.push("collapsed management");
    await frame().click('[data-handler="openCompanion"]');
    await checkScreen("companion", ".default-voice-page");
    assert.equal(await frame().$('[data-handler="chooseCompanion"]'), null);
    assert.equal(await frame().$(".wx-tabbar-host"), null);
    checks.push("default voice is a child page without a selector");
    await frame().click('[data-handler="goBack"]');
    await checkScreen("device", '[data-handler="openLimitsSheet"]');
    checks.push("default voice back");
    await frame().click('[data-handler="openLimitsSheet"]');
    await checkScreen("device-quiet", ".sheet-mask");
    checks.push("quiet hours sheet open");
    await frame().evaluate(() => document.querySelector('[data-handler="closeLimitsSheet"]').click());
    await checkScreen("device", ".wx-tabbar-host");
    checks.push("quiet hours sheet close");
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(path.dirname(source), "preview-checks.json"), `${JSON.stringify({ date: new Date().toISOString(), checks, errors }, null, 2)}\n`);
    console.log(`${checks.length} preview checks passed`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
