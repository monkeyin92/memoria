#!/usr/bin/env node
/**
 * Export each app.html screen into fixtures/<slug>.html at 390×844.
 * These fixtures are the visual contract the mini program WXML must match.
 * After WXML ports, re-export from page sources (or hand-sync) so "actual"
 * screenshots track the mini program, not the design HTML.
 */
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const DESIGN_HTML = path.join(
  __dirname,
  "../../../../docs/design/app-v4/app.html",
);
const FIXTURES = path.join(__dirname, "fixtures");
const NAMES = [
  "01-login",
  "02-consent",
  "03-home",
  "04-summary",
  "05-attention",
  "06-usage",
  "07-content",
  "08-roles",
  "09-memory",
  "10-profile",
];

async function main() {
  const chrome =
    process.env.CHROME_PATH ||
    (fs.existsSync("/usr/local/bin/google-chrome")
      ? "/usr/local/bin/google-chrome"
      : "/usr/bin/google-chrome");
  fs.mkdirSync(FIXTURES, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu"],
  });
  try {
    for (let i = 0; i < NAMES.length; i += 1) {
      const page = await browser.newPage();
      await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 1 });
      await page.goto(`file://${DESIGN_HTML}?p=${i + 1}`, {
        waitUntil: "networkidle0",
      });
      const inner = await page.evaluate(() => {
        const root = document.querySelector("#root .s");
        return root ? root.innerHTML : "";
      });
      const styles = await page.evaluate(() => {
        const style = document.querySelector("style");
        return style ? style.textContent : "";
      });
      const html = `<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=390, initial-scale=1">
<title>${NAMES[i]} fixture</title>
<style>${styles}
html,body{margin:0;padding:0;background:#F4F4F7}
body{width:390px;height:844px;overflow:hidden}
.s{width:390px;height:844px;position:relative;overflow:hidden;background:var(--bg)}
</style>
<body><div class="s">${inner}</div></body></html>`;
      const out = path.join(FIXTURES, `${NAMES[i]}.html`);
      fs.writeFileSync(out, html);
      console.error("wrote", out);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
