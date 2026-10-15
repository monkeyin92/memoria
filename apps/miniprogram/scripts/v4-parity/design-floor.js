#!/usr/bin/env node
/*
 * Renders the design source (docs/design/app-v4/app.html) in this machine's
 * Chrome and diffs each screen against the PNG it was exported to. Layout is
 * identical, so the diffRatio is font and rasteriser drift alone: the floor a
 * live page cannot beat on this machine.
 *   node scripts/v4-parity/design-floor.js [slug ...]
 */
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const { launch } = require("../mp-harness/shoot");
const { PARITY } = require("./scenarios");

const DESIGN = path.join(__dirname, "../../../../docs/design/app-v4");

async function main() {
  const only = process.argv.slice(2);
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "v4-floor-"));
  const browser = await launch();
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2 });
    for (const scenario of PARITY.filter((item) => !only.length || only.includes(item.slug))) {
      await page.goto(`file://${DESIGN}/app.html?p=${Number.parseInt(scenario.design, 10)}`, { waitUntil: "load" });
      await page.evaluate(() => document.fonts.ready);
      const shot = path.join(scratch, `${scenario.slug}.png`);
      await page.screenshot({ path: shot, clip: { x: 0, y: 0, width: 390, height: 844 } });
      const result = spawnSync(
        process.execPath,
        [path.join(__dirname, "compare-side-by-side.js"), "--actual", shot, "--design", path.join(DESIGN, scenario.design), "--out", path.join(scratch, `side-${scenario.slug}.png`)],
        { encoding: "utf8" },
      );
      if (result.status !== 0) throw new Error(result.stderr || result.stdout || "compare failed");
      const { diffRatio } = JSON.parse(result.stdout.trim().split("\n").pop());
      console.log(`${scenario.slug}: floor diffRatio=${diffRatio}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
