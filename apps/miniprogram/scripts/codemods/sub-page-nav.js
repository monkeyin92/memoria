#!/usr/bin/env node
/*
 * Moves a secondary page to custom navigation with the v4 round back button:
 * json navigationStyle custom, statusBarStyle data, goBack via leavePage,
 * and the in-page back row replaced by .nav-back. Idempotent.
 *   node scripts/codemods/sub-page-nav.js
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const HEADING_ONLY = new Set(["child-memory"]);
const PAGES = {
  guardian: "/pages/profile/index",
  bind: "/pages/profile/index",
  "persona-custom": "/pages/profile/index",
  auth: "/pages/home/index",
  privacy: "/pages/profile/index",
  "digital-self": "/pages/profile/index",
  "device-onboarding": "/pages/device/index",
};
for (const page of HEADING_ONLY) {
  if (Object.prototype.hasOwnProperty.call(PAGES, page)) {
    throw new Error(`sub-page-nav: ${page} is heading-only and must not be in PAGES`);
  }
}
const NAV_BACK =
  '<view class="nav-back" bindtap="goBack" aria-label="返回"><image class="nav-back-icon" src="/assets/ui/v4/back-strong.svg" /></view>';

function edit(file, fn) {
  const before = fs.readFileSync(file, "utf8");
  const after = fn(before);
  if (after !== before) fs.writeFileSync(file, after);
  return after !== before;
}

function migrateJson(source) {
  const json = JSON.parse(source);
  if (json.navigationStyle === "custom") return source;
  const out = {};
  for (const [key, value] of Object.entries(json)) {
    if (key === "navigationBarBackgroundColor") continue;
    out[key] = value;
    if (key === "navigationBarTitleText") out.navigationStyle = "custom";
  }
  if (!out.navigationStyle) out.navigationStyle = "custom";
  return `${JSON.stringify(out, null, 2)}\n`;
}

function migrateJs(source, fallback) {
  let out = source;
  if (!out.includes("utils/nav-inset")) {
    out = out.replace(/^(const [^\n]+require\([^\n]+\);\n)/m, `$1const { statusBarStyle } = require("../../utils/nav-inset");\nconst { leavePage } = require("../../utils/nav-back");\n`);
  }
  if (!/statusBarStyle: statusBarStyle\(\)/.test(out)) {
    out = out.replace(/\nPage\(\{\n  data: \{\n/, "\nPage({\n  data: {\n    statusBarStyle: statusBarStyle(),\n");
  }
  const goBack = `  goBack() {\n    leavePage("${fallback}");\n  },\n`;
  if (/\n  goBack\(\) \{\n[\s\S]*?\n  \},\n/.test(out)) {
    out = out.replace(/\n  goBack\(\) \{\n[\s\S]*?\n  \},\n/, `\n${goBack}`);
  } else {
    const dataStart = out.indexOf("\n  data: {\n");
    const dataEnd = out.indexOf("\n  },\n", dataStart);
    out = `${out.slice(0, dataEnd + 5)}\n\n${goBack.trimEnd()}${out.slice(dataEnd + 5)}`;
  }
  return out;
}

function migrateWxml(source) {
  let out = source;
  out = out.replace(/^\s*<view class="back-row">.*<\/view>\n/m, "");
  out = out.replace(/^\s*<view class="nav-back" bindtap="goBack"><text>[^<]*<\/text><\/view>\n/m, "");
  if (!out.includes('class="nav-back"')) {
    out = out.replace(/^<view class="([^"]*)"([^>]*)>\n/, (_, cls, rest) => {
      const style = rest.includes("statusBarStyle") ? rest : `${rest} style="{{statusBarStyle}}"`;
      return `<view class="${cls} sub-page"${style}>\n  ${NAV_BACK}\n`;
    });
  }
  return out;
}

for (const [page, fallback] of Object.entries(PAGES)) {
  const base = path.join(ROOT, "pages", page, "index");
  const changed = [
    edit(`${base}.json`, migrateJson) && "json",
    edit(`${base}.js`, (s) => migrateJs(s, fallback)) && "js",
    edit(`${base}.wxml`, migrateWxml) && "wxml",
  ].filter(Boolean);
  console.log(`${page}: ${changed.join(", ") || "unchanged"}`);
}
