#!/usr/bin/env node
/*
 * Bytes of the files the mini program ships, before the upload minifier.
 *   node scripts/package-size.js
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");

function ignoredUploadFolders() {
  const config = JSON.parse(fs.readFileSync(path.join(ROOT, "project.config.example.json"), "utf8"));
  const folders = (config.packOptions?.ignore || [])
    .filter((entry) => entry && entry.type === "folder" && typeof entry.value === "string")
    .map((entry) => entry.value);
  return new Set([...folders, "scripts", "node_modules"]);
}

const SKIP_DIRS = ignoredUploadFolders();
const SKIP_FILE = /^(package(-lock)?\.json|project\.config.*\.json)$|\.md$/;
const MAIN_PACKAGE_LIMIT = 2 * 1024 * 1024;

function shippedFiles(dir = "") {
  return fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true }).flatMap((entry) => {
    const rel = path.posix.join(dir, entry.name);
    if (entry.isDirectory()) return SKIP_DIRS.has(rel) ? [] : shippedFiles(rel);
    return SKIP_FILE.test(rel) ? [] : [rel];
  });
}

function packageSize() {
  const byFolder = {};
  let total = 0;
  for (const file of shippedFiles()) {
    const bytes = fs.statSync(path.join(ROOT, file)).size;
    const folder = file.includes("/") ? file.split("/").slice(0, 2).join("/") : file;
    byFolder[folder] = (byFolder[folder] || 0) + bytes;
    total += bytes;
  }
  return { total, limit: MAIN_PACKAGE_LIMIT, byFolder };
}

module.exports = { MAIN_PACKAGE_LIMIT, packageSize, shippedFiles };

if (require.main === module) {
  const { total, limit, byFolder } = packageSize();
  for (const [folder, bytes] of Object.entries(byFolder).sort((a, b) => b[1] - a[1]).slice(0, 10)) {
    console.log(`${(bytes / 1024).toFixed(1).padStart(8)} KiB  ${folder}`);
  }
  console.log(`${(total / 1024).toFixed(1)} KiB of ${limit / 1024} KiB main package limit`);
}
