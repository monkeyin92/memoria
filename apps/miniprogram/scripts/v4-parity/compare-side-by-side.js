#!/usr/bin/env node
/**
 * Build side-by-side actual|design images and pixel-diff metrics via Chrome.
 * Usage:
 *   node compare-side-by-side.js --actual a.png --design d.png --out side.png --json m.json
 */
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    out[key.slice(2)] = argv[i + 1];
    i += 1;
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.actual || !args.design || !args.out) {
    console.error(
      "usage: compare-side-by-side.js --actual a.png --design d.png --out side.png [--json m.json] [--width 390] [--height 844]",
    );
    process.exit(2);
  }
  const width = Number(args.width || 780);
  const height = Number(args.height || 1688);
  const gap = 16;
  const chrome =
    process.env.CHROME_PATH ||
    (fs.existsSync("/usr/local/bin/google-chrome")
      ? "/usr/local/bin/google-chrome"
      : "/usr/bin/google-chrome");

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: "new",
    args: [
      "--no-sandbox",
      "--disable-gpu",
      "--font-render-hinting=none",
      "--allow-file-access-from-files",
    ],
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({
      width: width * 2 + gap,
      height,
      deviceScaleFactor: 1,
    });
    await page.setContent(
      `<!doctype html><html><body>
      <div class="row" style="display:flex;gap:${gap}px">
        <img id="actual" width="${width}" height="${height}">
        <img id="design" width="${width}" height="${height}">
      </div>
      <canvas id="c"></canvas>
      <style>html,body{margin:0;background:#E6E6EC} img{object-fit:cover;background:#F4F4F7}</style>
      </body></html>`,
      { waitUntil: "load" },
    );
    const actualB64 = fs.readFileSync(args.actual).toString("base64");
    const designB64 = fs.readFileSync(args.design).toString("base64");
    const actualMime = args.actual.endsWith(".jpg") || args.actual.endsWith(".jpeg")
      ? "image/jpeg"
      : "image/png";
    const designMime = args.design.endsWith(".jpg") || args.design.endsWith(".jpeg")
      ? "image/jpeg"
      : "image/png";
    await page.evaluate(
      async (aB64, aMime, dB64, dMime) => {
        const load = (img, b64, mime) =>
          new Promise((res, rej) => {
            img.onload = () => res();
            img.onerror = rej;
            img.src = `data:${mime};base64,${b64}`;
          });
        await Promise.all([
          load(document.getElementById("actual"), aB64, aMime),
          load(document.getElementById("design"), dB64, dMime),
        ]);
      },
      actualB64,
      actualMime,
      designB64,
      designMime,
    );
    const metrics = await page.evaluate((w, h) => {
      const a = document.getElementById("actual");
      const d = document.getElementById("design");
      const c = document.getElementById("c");
      c.width = w;
      c.height = h;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(a, 0, 0, w, h);
      const ad = ctx.getImageData(0, 0, w, h).data;
      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(d, 0, 0, w, h);
      const dd = ctx.getImageData(0, 0, w, h).data;
      let diffPixels = 0;
      let sumSq = 0;
      const n = w * h;
      for (let i = 0; i < n; i += 1) {
        const o = i * 4;
        const dr = ad[o] - dd[o];
        const dg = ad[o + 1] - dd[o + 1];
        const db = ad[o + 2] - dd[o + 2];
        const da = ad[o + 3] - dd[o + 3];
        const dist = dr * dr + dg * dg + db * db + da * da;
        sumSq += dist;
        if (dist > 25) diffPixels += 1;
      }
      return {
        width: w,
        height: h,
        diffPixels,
        diffRatio: diffPixels / n,
        rmse: Math.sqrt(sumSq / (n * 4)),
      };
    }, width, height);
    fs.mkdirSync(path.dirname(args.out), { recursive: true });
    await page.screenshot({ path: args.out, type: "png" });
    if (args.json) {
      fs.mkdirSync(path.dirname(args.json), { recursive: true });
      fs.writeFileSync(args.json, `${JSON.stringify(metrics, null, 2)}\n`);
    }
    console.log(
      JSON.stringify({
        out: args.out,
        diffRatio: Number(metrics.diffRatio.toFixed(6)),
        diffPixels: metrics.diffPixels,
        rmse: Number(metrics.rmse.toFixed(3)),
      }),
    );
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
