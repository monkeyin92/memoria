/*
 * Chrome screenshots of harness-rendered pages plus layout measurements:
 * horizontal overflow, content hidden under the tab bar, and taps under the
 * WeChat capsule on custom-navigation pages.
 */
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const { loadPage } = require("./runtime");
const { renderPage } = require("./render");

const HTML_DIR = path.join(os.tmpdir(), "mp-harness-html");

function chromePath() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  return fs.existsSync("/usr/local/bin/google-chrome") ? "/usr/local/bin/google-chrome" : "/usr/bin/google-chrome";
}

function launch() {
  return puppeteer.launch({
    executablePath: chromePath(),
    headless: "new",
    args: ["--no-sandbox", "--disable-gpu", "--font-render-hinting=none", "--allow-file-access-from-files"],
  });
}

async function scenarioData(scenario, view = {}) {
  const started = process.hrtime.bigint();
  const systemInfo = view.width
    ? { windowWidth: view.width, screenWidth: view.width, windowHeight: view.height, screenHeight: view.height, statusBarHeight: view.safeTop }
    : {};
  const harness = loadPage(scenario.page, { ...(scenario.harness || {}), systemInfo });
  try {
    await harness.run(scenario.query || {});
    const loadMs = Number(process.hrtime.bigint() - started) / 1e6;
    for (const [method, event] of scenario.actions || []) {
      await harness.page[method](event || { currentTarget: { dataset: {} }, detail: {} });
      await harness.settle();
    }
    return { data: harness.page.data, setDataLog: harness.setDataLog, loadMs };
  } finally {
    harness.restore();
  }
}

async function measure(page, { width, safeTop }) {
  return page.evaluate(
    ({ width, safeTop }) => {
      const doc = document.documentElement;
      const clipped = (el) => {
        for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (/(hidden|auto|scroll|clip)/.test(style.overflowX)) {
            const rect = node.getBoundingClientRect();
            if (rect.right <= width + 1 && rect.left >= -1) return true;
          }
        }
        return false;
      };
      const label = (el) => `${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? `.${el.className.trim().split(/\s+/).join(".")}` : ""}`;
      const overflow = [];
      for (const el of document.querySelectorAll(".mp-page *")) {
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height || getComputedStyle(el).position === "fixed") continue;
        if ((rect.right > width + 1 || rect.left < -1) && !clipped(el)) overflow.push(`${label(el)} ${Math.round(rect.left)}..${Math.round(rect.right)}`);
      }
      const tabHost = document.querySelector(".wx-tabbar-host");
      let tabClearance = null;
      if (tabHost) {
        const shell = document.querySelector(".mp-page > view") || document.querySelector(".mp-page");
        let contentBottom = 0;
        for (const el of shell.querySelectorAll("*")) {
          const style = getComputedStyle(el);
          if (style.position === "fixed" || style.display === "none" || style.visibility === "hidden") continue;
          const rect = el.getBoundingClientRect();
          if (rect.height) contentBottom = Math.max(contentBottom, rect.bottom + window.scrollY);
        }
        const tabHeight = tabHost.getBoundingClientRect().height;
        tabClearance = { tabHeight: Math.round(tabHeight), gap: Math.round(doc.scrollHeight - contentBottom) };
      }
      const capsule = { left: width - 94, right: width - 7, top: safeTop + 4, bottom: safeTop + 36 };
      const underCapsule = [];
      if (document.body.dataset.nav === "custom") {
        for (const el of document.querySelectorAll("[data-tap]")) {
          const rect = el.getBoundingClientRect();
          if (!rect.width || !rect.height) continue;
          const hit = rect.left < capsule.right && rect.right > capsule.left && rect.top + window.scrollY < capsule.bottom && rect.bottom + window.scrollY > capsule.top;
          if (hit) underCapsule.push(label(el));
        }
      }
      return { scrollWidth: doc.scrollWidth, scrollHeight: doc.scrollHeight, overflow: overflow.slice(0, 12), tabClearance, underCapsule };
    },
    { width, safeTop },
  );
}

async function shoot(browser, scenario, view, outPath) {
  const { data, setDataLog, loadMs } = await scenarioData(scenario, view);
  const renderStarted = process.hrtime.bigint();
  const html = renderPage(scenario.page, data, { width: view.width, safeTop: view.safeTop, safeBottom: view.safeBottom, fontScale: view.fontScale || 1 });
  const renderMs = Number(process.hrtime.bigint() - renderStarted) / 1e6;
  fs.mkdirSync(HTML_DIR, { recursive: true });
  const htmlPath = path.join(HTML_DIR, `${path.basename(outPath, ".png")}.html`);
  fs.writeFileSync(htmlPath, html);
  const page = await browser.newPage();
  try {
    await page.setViewport({ width: view.width, height: view.height, deviceScaleFactor: view.dpr || 1 });
    const layoutStarted = Date.now();
    await page.goto(`file://${htmlPath}`, { waitUntil: "load" });
    await page.evaluate(() => document.fonts.ready);
    const layoutMs = Date.now() - layoutStarted;
    const metrics = await measure(page, view);
    if (view.fullPage && metrics.scrollHeight > view.height) {
      await page.setViewport({ width: view.width, height: metrics.scrollHeight, deviceScaleFactor: view.dpr || 1 });
    }
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    await page.screenshot({ path: outPath, type: "png" });
    const setDataBytes = setDataLog.reduce((sum, entry) => sum + entry.bytes, 0);
    return { ...metrics, loadMs: +loadMs.toFixed(1), renderMs: +renderMs.toFixed(1), layoutMs, setDataCalls: setDataLog.length, setDataBytes, htmlPath };
  } finally {
    await page.close();
  }
}

module.exports = { launch, scenarioData, shoot };
