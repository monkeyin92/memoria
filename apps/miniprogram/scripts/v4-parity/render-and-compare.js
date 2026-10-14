#!/usr/bin/env node
/*
 * Renders each live mini-program page state mapped in scenarios.js at the
 * design frame (390×844 CSS @2x, iPhone 14 safe areas) and writes
 * mp-screenshots/{actual,side-by-side,metrics}/<slug> against the design PNG.
 *   node scripts/v4-parity/render-and-compare.js [slug ...]
 */
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const { launch, shoot } = require("../mp-harness/shoot");
const { PARITY } = require("./scenarios");

process.env.TZ = "Asia/Shanghai";

const REPO = path.join(__dirname, "../../../..");
const DESIGN = path.join(REPO, "docs/design/app-v4");
const OUT = path.join(DESIGN, "mp-screenshots");
const FRAME = { width: 390, height: 844, dpr: 2, safeTop: 47, safeBottom: 34 };

function compare(actual, design, sideOut, jsonOut) {
  const result = spawnSync(
    process.execPath,
    [path.join(__dirname, "compare-side-by-side.js"), "--actual", actual, "--design", design, "--out", sideOut, "--json", jsonOut, "--width", "780", "--height", "1688"],
    { encoding: "utf8" },
  );
  if (result.status !== 0) throw new Error(result.stderr || result.stdout || "compare failed");
  return JSON.parse(result.stdout.trim().split("\n").pop());
}

async function main() {
  const only = process.argv.slice(2);
  const browser = await launch();
  const summary = [];
  try {
    for (const scenario of PARITY.filter((item) => !only.length || only.includes(item.slug))) {
      const actualPath = path.join(OUT, "actual", `${scenario.slug}.png`);
      const layout = await shoot(browser, scenario, FRAME, actualPath);
      const metrics = compare(actualPath, path.join(DESIGN, scenario.design), path.join(OUT, "side-by-side", `${scenario.slug}.png`), path.join(OUT, "metrics", `${scenario.slug}.json`));
      const { htmlPath, ...kept } = layout;
      summary.push({ slug: scenario.slug, page: scenario.page, diffRatio: metrics.diffRatio, rmse: metrics.rmse, overflow: kept.overflow, underCapsule: kept.underCapsule, tabClearance: kept.tabClearance });
      console.error(`${scenario.slug}: diffRatio=${metrics.diffRatio} rmse=${metrics.rmse} overflow=${kept.overflow.length} underCapsule=${kept.underCapsule.length}`);
    }
  } finally {
    await browser.close();
  }
  if (!only.length) fs.writeFileSync(path.join(OUT, "compare-summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
