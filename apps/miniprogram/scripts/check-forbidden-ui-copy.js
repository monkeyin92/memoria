#!/usr/bin/env node
/*
 * Fail CI when parent-visible mini program copy uses paused or banned words.
 * Scans wxml and user-facing string literals in page/component js.
 * Keep the word list in sync with docs/design/app-v4/design-notes.md.
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const FORBIDDEN = ["危机", "伙伴", "好朋友", "家人", "推送", "通知"];
const SKIP_DIR_NAMES = new Set(["node_modules", "tests", "generated", ".git"]);
const SCAN_EXT = new Set([".wxml", ".js", ".json"]);

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIR_NAMES.has(entry.name)) continue;
      walk(full, out);
      continue;
    }
    if (SCAN_EXT.has(path.extname(entry.name))) out.push(full);
  }
  return out;
}

function isCommentOnlyHit(line, word) {
  const trimmed = line.trim();
  if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*")) {
    return trimmed.includes(word);
  }
  return false;
}

function scanFile(file) {
  const rel = path.relative(ROOT, file);
  // Internal modules may keep identifiers; only page/component surfaces and utils
  // that export user-visible copy are gated. Skip pure test helpers.
  if (rel.startsWith("utils/") && !/(guardian-push|crisis-alerts|trial-device-card|device-binding)\.js$/.test(rel)) {
    return [];
  }
  if (rel === "scripts/check-forbidden-ui-copy.js") return [];
  const text = fs.readFileSync(file, "utf8");
  const hits = [];
  const lines = text.split(/\r?\n/);
  lines.forEach((line, index) => {
    for (const word of FORBIDDEN) {
      if (!line.includes(word)) continue;
      if (isCommentOnlyHit(line, word)) continue;
      // Allow identifier-like uses in non-wxml only when clearly not UI copy:
      // skipped none — product rule is zero visible use.
      hits.push({ file: rel, line: index + 1, word, text: line.trim().slice(0, 160) });
    }
  });
  return hits;
}

function main() {
  const files = [
    ...walk(path.join(ROOT, "pages")),
    ...walk(path.join(ROOT, "components")),
    ...walk(path.join(ROOT, "custom-tab-bar")),
    path.join(ROOT, "utils/guardian-push.js"),
    path.join(ROOT, "utils/crisis-alerts.js"),
    path.join(ROOT, "utils/trial-device-card.js"),
    path.join(ROOT, "utils/device-binding.js"),
    path.join(ROOT, "app.json"),
  ].filter((file) => fs.existsSync(file));

  const hits = files.flatMap(scanFile);
  if (hits.length) {
    console.error("Forbidden UI copy found:");
    for (const hit of hits) {
      console.error(`  ${hit.file}:${hit.line} [${hit.word}] ${hit.text}`);
    }
    process.exit(1);
  }
  console.log(`Forbidden UI copy check passed (${files.length} files, words: ${FORBIDDEN.join("/")}).`);
}

main();
