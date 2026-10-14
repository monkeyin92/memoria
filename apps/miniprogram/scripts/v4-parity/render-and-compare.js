#!/usr/bin/env node
/**
 * Render each mapped mini-program page fixture in Chrome, screenshot,
 * and write side-by-side comparisons against docs/design/app-v4 baselines.
 *
 * Fixtures live in scripts/v4-parity/fixtures/<slug>.html and must mirror the
 * live WXML layout for that page (kept in sync by the restyle).
 */
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const puppeteer = require("puppeteer-core");

const ROOT = path.join(__dirname, "../..");
const REPO = path.join(ROOT, "../..");
const DESIGN = path.join(REPO, "docs/design/app-v4");
const OUT = path.join(DESIGN, "mp-screenshots");
const FIXTURES = path.join(__dirname, "fixtures");

const PAGES = [
  { slug: "01-login", design: "01-login.png", fixture: "01-login.html" },
  { slug: "02-consent", design: "02-consent.png", fixture: "02-consent.html" },
  { slug: "03-home", design: "03-home.png", fixture: "03-home.html" },
  { slug: "04-summary", design: "04-summary.png", fixture: "04-summary.html" },
  { slug: "05-attention", design: "05-attention.png", fixture: "05-attention.html" },
  { slug: "06-usage", design: "06-usage.png", fixture: "06-usage.html" },
  { slug: "07-content", design: "07-content.png", fixture: "07-content.html" },
  { slug: "08-roles", design: "08-roles.png", fixture: "08-roles.html" },
  { slug: "09-memory", design: "09-memory.png", fixture: "09-memory.html" },
  { slug: "10-profile", design: "10-profile.png", fixture: "10-profile.html" },
];

const WIDTH = 780;
const HEIGHT = 1688;
const CSS_WIDTH = 390;
const CSS_HEIGHT = 844;

function chromePath() {
  return (
    process.env.CHROME_PATH ||
    (fs.existsSync("/usr/local/bin/google-chrome")
      ? "/usr/local/bin/google-chrome"
      : "/usr/bin/google-chrome")
  );
}

async function screenshotFixture(browser, fixturePath, outPath) {
  const page = await browser.newPage();
  // Match design PNGs at 780×1688 (390×844 CSS @2x).
  await page.setViewport({
    width: CSS_WIDTH,
    height: CSS_HEIGHT,
    deviceScaleFactor: 2,
  });
  await page.goto(`file://${fixturePath}`, { waitUntil: "networkidle0" });
  await page.evaluate(async () => {
    await Promise.all(
      Array.from(document.images).map((img) =>
        img.complete
          ? null
          : new Promise((res) => {
              img.onload = res;
              img.onerror = res;
            }),
      ),
    );
  });
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  await page.screenshot({ path: outPath, type: "png" });
  await page.close();
}

function compare(actual, design, sideOut, jsonOut) {
  const script = path.join(__dirname, "compare-side-by-side.js");
  const result = spawnSync(
    process.execPath,
    [
      script,
      "--actual",
      actual,
      "--design",
      design,
      "--out",
      sideOut,
      "--json",
      jsonOut,
      "--width",
      String(WIDTH),
      "--height",
      String(HEIGHT),
    ],
    { encoding: "utf8", cwd: __dirname },
  );
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || "compare failed");
  }
  return JSON.parse(result.stdout.trim().split("\n").pop());
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: chromePath(),
    headless: "new",
    args: [
      "--no-sandbox",
      "--disable-gpu",
      "--font-render-hinting=none",
      "--allow-file-access-from-files",
    ],
  });
  const summary = [];
  try {
    for (const pageSpec of PAGES) {
      const fixture = path.join(FIXTURES, pageSpec.fixture);
      if (!fs.existsSync(fixture)) {
        summary.push({ slug: pageSpec.slug, status: "MISSING_FIXTURE" });
        continue;
      }
      const actualPath = path.join(OUT, "actual", `${pageSpec.slug}.png`);
      const sidePath = path.join(OUT, "side-by-side", `${pageSpec.slug}.png`);
      const jsonPath = path.join(OUT, "metrics", `${pageSpec.slug}.json`);
      await screenshotFixture(browser, fixture, actualPath);
      const metrics = compare(
        actualPath,
        path.join(DESIGN, pageSpec.design),
        sidePath,
        jsonPath,
      );
      summary.push({ slug: pageSpec.slug, status: "OK", ...metrics });
      console.error(`${pageSpec.slug}: diffRatio=${metrics.diffRatio}`);
    }
  } finally {
    await browser.close();
  }
  const reportPath = path.join(OUT, "compare-summary.json");
  fs.writeFileSync(reportPath, `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ reportPath, summary }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
