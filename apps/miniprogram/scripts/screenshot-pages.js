#!/usr/bin/env node
/*
 * Screenshots every page state in scripts/mp-harness/pages.js at 320, 375, 390
 * and 430 widths plus a 1.3x font scale, and records layout checks and timings to
 * docs/design/app-v4/mp-screenshots/pages/results.json.
 *   node scripts/screenshot-pages.js [slug ...]
 */
const fs = require("node:fs");
const path = require("node:path");

const { launch, problems, shoot } = require("./mp-harness/shoot");
const { PAGES, VIEWS } = require("./mp-harness/pages");

process.env.TZ = "Asia/Shanghai";

const OUT = path.join(__dirname, "../../../docs/design/app-v4/mp-screenshots/pages");

async function main() {
  const only = process.argv.slice(2);
  const browser = await launch();
  const results = [];
  try {
    for (const scenario of PAGES.filter((item) => !only.length || only.includes(item.slug))) {
      for (const view of VIEWS) {
        const file = path.join(OUT, `${scenario.slug}-${view.name}.png`);
        const { htmlPath, ...result } = await shoot(browser, scenario, view, file);
        const issues = problems(result, view);
        results.push({ slug: scenario.slug, view: view.name, issues, ...result });
        console.error(`${scenario.slug}@${view.name}: ${issues.length ? issues.join(" | ") : "ok"} load=${result.loadMs}ms render=${result.renderMs}ms layout=${result.layoutMs}ms setData=${result.setDataCalls}/${result.setDataBytes}B`);
      }
    }
  } finally {
    await browser.close();
  }
  if (!only.length) fs.writeFileSync(path.join(OUT, "results.json"), `${JSON.stringify(results, null, 2)}\n`);
  const failing = results.filter((item) => item.issues.length);
  console.log(`${results.length} shots, ${failing.length} with layout issues`);
  if (failing.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
