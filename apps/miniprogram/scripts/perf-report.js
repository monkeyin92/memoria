#!/usr/bin/env node
/*
 * Writes docs/design/app-v4/mp-screenshots/perf.json: shipped package size,
 * per-page load/render/layout/setData at 390pt, and for the 200-item lists the
 * setData size plus scroll frame intervals over three runs.
 *   node scripts/perf-report.js
 */
const fs = require("node:fs");
const path = require("node:path");

process.env.TZ = "Asia/Shanghai";

const { launch, scenarioData, scrollFrames, shoot } = require("./mp-harness/shoot");
const { LONG_LISTS, PAGES, VIEWS } = require("./mp-harness/pages");
const { packageSize } = require("./package-size");

const OUT = path.join(__dirname, "../../../docs/design/app-v4/mp-screenshots/perf.json");
const VIEW = VIEWS.find((view) => view.name === "390");
const SCROLL_RUNS = 3;

const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

async function setDataStats(scenario) {
  const { setDataLog } = await scenarioData(scenario, VIEW);
  const bytes = setDataLog.map((entry) => entry.bytes);
  return { setDataCalls: bytes.length, setDataLargest: Math.max(0, ...bytes), setDataTotal: bytes.reduce((a, b) => a + b, 0) };
}

async function main() {
  const browser = await launch();
  const pages = [];
  const longLists = [];
  try {
    for (const scenario of PAGES) {
      const { loadMs, renderMs, layoutMs } = await shoot(browser, scenario, VIEW, null);
      pages.push({ slug: scenario.slug, loadMs, renderMs, layoutMs, ...(await setDataStats(scenario)) });
    }
    for (const scenario of LONG_LISTS) {
      const runs = [];
      for (let run = 0; run < SCROLL_RUNS; run += 1) runs.push(await scrollFrames(browser, scenario, VIEW));
      longLists.push({
        slug: scenario.slug,
        ...(await setDataStats(scenario)),
        scrollHeight: runs[0].scrollHeight,
        renderMs: median(runs.map((r) => r.renderMs)),
        layoutMs: median(runs.map((r) => r.layoutMs)),
        frames: runs[0].frames,
        p50FrameMs: median(runs.map((r) => r.p50FrameMs)),
        p95FrameMs: median(runs.map((r) => r.p95FrameMs)),
        maxFrameMs: Math.max(...runs.map((r) => r.maxFrameMs)),
      });
    }
  } finally {
    await browser.close();
  }
  const { total, limit, byFolder } = packageSize();
  const report = { view: VIEW.name, packageSize: { bytes: total, limitBytes: limit, byFolder }, pages, longLists };
  fs.writeFileSync(OUT, `${JSON.stringify(report, null, 2)}\n`);
  console.log(`package ${(total / 1024).toFixed(1)} KiB / ${limit / 1024} KiB`);
  for (const item of longLists) {
    console.log(`${item.slug}: setData ${item.setDataLargest}B, layout ${item.layoutMs}ms, scroll p50 ${item.p50FrameMs}ms p95 ${item.p95FrameMs}ms max ${item.maxFrameMs}ms over ${item.frames} frames`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
