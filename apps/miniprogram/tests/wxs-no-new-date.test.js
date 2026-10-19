const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.join(__dirname, "..");
const SKIP_DIRS = new Set(["node_modules", "generated", "design-preview"]);
const NEW_DATE = /\bnew\s+Date\b/g;
const INLINE_WXS = /<wxs\b[^>]*>([\s\S]*?)<\/wxs>/gi;

function lineAt(source, index) {
  let line = 1;
  for (let i = 0; i < index; i += 1) {
    if (source[i] === "\n") line += 1;
  }
  return line;
}

function newDateIndices(source) {
  const indices = [];
  NEW_DATE.lastIndex = 0;
  let match;
  while ((match = NEW_DATE.exec(source)) !== null) {
    indices.push(match.index);
  }
  return indices;
}

function newDateLines(source) {
  return newDateIndices(source).map((index) => lineAt(source, index));
}

function inlineWxsBlocks(source) {
  const blocks = [];
  INLINE_WXS.lastIndex = 0;
  let match;
  while ((match = INLINE_WXS.exec(source)) !== null) {
    const contentStart = match.index + match[0].indexOf(">") + 1;
    blocks.push({ content: match[1], contentStart });
  }
  return blocks;
}

function findNewDateViolations(root) {
  const violations = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith(".") || SKIP_DIRS.has(entry.name)) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!entry.name.endsWith(".wxs") && !entry.name.endsWith(".wxml")) continue;

      const source = fs.readFileSync(full, "utf8");
      const relative = path.relative(root, full).split(path.sep).join("/");
      if (entry.name.endsWith(".wxs")) {
        for (const index of newDateIndices(source)) {
          violations.push(`${relative}:${lineAt(source, index)}`);
        }
        continue;
      }
      for (const block of inlineWxsBlocks(source)) {
        for (const index of newDateIndices(block.content)) {
          violations.push(`${relative}:${lineAt(source, block.contentStart + index)}`);
        }
      }
    }
  };
  walk(root);
  return violations;
}

test("WXS files and inline blocks never call new Date", () => {
  const violations = findNewDateViolations(ROOT);
  assert.deepEqual(
    violations,
    [],
    `WXS must use getDate(...) instead of new Date(...):\n${violations.join("\n")}`,
  );
});

test("new Date detector catches calls without flagging getDate", () => {
  assert.deepEqual(newDateLines("var date = new Date(x);"), [1]);
  assert.deepEqual(
    newDateLines("var day = date.getDate();\nvar date = getDate(x);"),
    [],
  );
  assert.deepEqual(
    newDateLines("var day = date.getDate();\nvar date = getDate(x);\nvar other = new  Date(y);"),
    [3],
  );
});
