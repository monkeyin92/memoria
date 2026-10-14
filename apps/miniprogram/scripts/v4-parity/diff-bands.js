#!/usr/bin/env node
/*
 * Splits each actual|design pair into horizontal bands and prints the
 * diffRatio of each band, with the same per-pixel threshold as
 * compare-side-by-side.js, to show where on the screen a residual sits.
 * --shift moves the actual image up by that many device px before the diff,
 * to test whether a band differs only by a vertical offset.
 *   node scripts/v4-parity/diff-bands.js [--band 211] [--shift 0] [slug ...]
 */
const fs = require("node:fs");
const path = require("node:path");

const { launch } = require("../mp-harness/shoot");
const { PARITY } = require("./scenarios");

const DESIGN = path.join(__dirname, "../../../../docs/design/app-v4");
const WIDTH = 780;
const HEIGHT = 1688;

function parseArgs(argv) {
  const args = { band: 211, shift: 0, only: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--band") args.band = Number(argv[++i]);
    else if (argv[i] === "--shift") args.shift = Number(argv[++i]);
    else args.only.push(argv[i]);
  }
  return args;
}

const dataUrl = (file) => `data:image/png;base64,${fs.readFileSync(file).toString("base64")}`;

async function main() {
  const { band, shift, only } = parseArgs(process.argv.slice(2));
  const browser = await launch();
  try {
    const page = await browser.newPage();
    for (const scenario of PARITY.filter((item) => !only.length || only.includes(item.slug))) {
      const actual = path.join(DESIGN, "mp-screenshots/actual", `${scenario.slug}.png`);
      const ratios = await page.evaluate(async (a, d, w, h, size, dy) => {
        const load = (src) => new Promise((resolve, reject) => {
          const img = new Image();
          img.onload = () => resolve(img);
          img.onerror = reject;
          img.src = src;
        });
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        const pixels = async (src, top) => {
          ctx.clearRect(0, 0, w, h);
          ctx.drawImage(await load(src), 0, -top, w, h);
          return ctx.getImageData(0, 0, w, h).data;
        };
        const ad = await pixels(a, dy);
        const dd = await pixels(d, 0);
        const out = [];
        for (let top = 0; top < h; top += size) {
          const bottom = Math.min(h, top + size);
          let diff = 0;
          for (let o = top * w * 4; o < bottom * w * 4; o += 4) {
            let dist = 0;
            for (let k = 0; k < 4; k += 1) dist += (ad[o + k] - dd[o + k]) ** 2;
            if (dist > 25) diff += 1;
          }
          out.push({ top, bottom, ratio: diff / ((bottom - top) * w) });
        }
        return out;
      }, dataUrl(actual), dataUrl(path.join(DESIGN, scenario.design)), WIDTH, HEIGHT, band, shift);
      console.log(`${scenario.slug}: ${ratios.map((r) => `${r.top}-${r.bottom}=${r.ratio.toFixed(4)}`).join(" ")}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
