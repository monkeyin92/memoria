#!/usr/bin/env node
/*
 * Lists classes a page's WXML uses that no stylesheet in scope defines
 * (app.wxss, imported page styles, the page wxss and its components).
 *   node scripts/mp-harness/class-audit.js [page-dir ...]
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");

function cssWithImports(file, seen = new Set()) {
  if (!fs.existsSync(file) || seen.has(file)) return "";
  seen.add(file);
  const css = fs.readFileSync(file, "utf8");
  return css.replace(/@import\s+["']([^"']+)["']\s*;?/g, (_, target) => cssWithImports(path.resolve(path.dirname(file), target), seen));
}

function definedClasses(css) {
  const out = new Set();
  for (const [, name] of css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) out.add(name);
  return out;
}

function usedClasses(wxml) {
  const out = new Set();
  for (const [, value] of wxml.matchAll(/\bclass="([^"]*)"/g)) {
    const literal = value.replace(/\{\{([\s\S]*?)\}\}/g, (_, expr) => ` ${[...expr.matchAll(/'([^']*)'/g)].map((m) => m[1]).join(" ")} `);
    for (const name of literal.split(/\s+/)) if (/^[_a-zA-Z][\w-]*$/.test(name)) out.add(name);
  }
  return out;
}

function auditPage(dir) {
  const base = path.join(ROOT, "pages", dir, "index");
  const using = JSON.parse(fs.readFileSync(`${base}.json`, "utf8")).usingComponents || {};
  let css = cssWithImports(path.join(ROOT, "app.wxss")) + cssWithImports(`${base}.wxss`);
  for (const target of Object.values(using)) css += cssWithImports(path.join(ROOT, `${target.replace(/^\//, "")}.wxss`));
  const defined = definedClasses(css);
  return [...usedClasses(fs.readFileSync(`${base}.wxml`, "utf8"))].filter((name) => !defined.has(name)).sort();
}

const dirs = process.argv.slice(2).length ? process.argv.slice(2) : fs.readdirSync(path.join(ROOT, "pages"));
let total = 0;
for (const dir of dirs) {
  const missing = auditPage(dir);
  total += missing.length;
  if (missing.length) console.log(`${dir}: ${missing.join(" ")}`);
}
console.log(`undefined classes: ${total}`);
module.exports = { auditPage };
