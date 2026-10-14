#!/usr/bin/env node
/*
 * Rewrites every rpx length in WXSS and WXML as px at half the value, so the
 * 390 pt design frame renders 1:1 and a 375 pt screen renders exactly as
 * before. Literal lengths become px; mustache lengths such as {{size}}rpx
 * become {{(size) / 2}}px. Idempotent: a second run finds no rpx.
 *   node scripts/codemods/rpx-to-px.js
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const SKIP = new Set(["node_modules", "design-preview", "scripts", "tests"]);

function files(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return SKIP.has(entry.name) ? [] : files(full);
    return /\.(wxss|wxml)$/.test(entry.name) ? [full] : [];
  });
}

function halve(text) {
  return text
    .replace(/\{\{([^{}]+)\}\}rpx/g, (_, expression) => `{{(${expression.trim()}) / 2}}px`)
    .replace(/(-?\d*\.?\d+)rpx/g, (_, number) => `${+(Number(number) / 2).toFixed(3)}px`);
}

function main() {
  let changed = 0;
  for (const file of files(ROOT)) {
    const before = fs.readFileSync(file, "utf8");
    const after = halve(before);
    if (after !== before) {
      fs.writeFileSync(file, after);
      changed += 1;
    }
  }
  console.log(`rpx-to-px: rewrote ${changed} files`);
}

if (require.main === module) main();

module.exports = { halve };
