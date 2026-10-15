const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { TILE_ICONS } = require("../components/mp-icon/tile-icons");

const root = path.join(__dirname, "..");
const pageDirs = fs.readdirSync(path.join(root, "pages"));
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8");
const sources = [
  ...pageDirs.flatMap((dir) => [`pages/${dir}/index.wxml`, `pages/${dir}/index.js`]),
  ...fs.readdirSync(path.join(root, "components")).flatMap((dir) => [`components/${dir}/index.wxml`, `components/${dir}/index.js`]),
  "custom-tab-bar/index.js",
  "custom-tab-bar/index.wxml",
].filter((rel) => fs.existsSync(path.join(root, rel)));

test("pages use v4 line icons, not the retired glass icons or photo backdrop", () => {
  for (const rel of sources) {
    assert.doesNotMatch(read(rel), /<glass-icon|<page-backdrop|assets\/ui\/(icons|bg)\//, rel);
  }
  for (const dir of pageDirs) {
    const using = JSON.parse(read(`pages/${dir}/index.json`)).usingComponents || {};
    assert.equal(using["glass-icon"], undefined, dir);
    assert.equal(using["page-backdrop"], undefined, dir);
  }
});

test("every mp-icon name and v4 asset path resolves to a generated file", () => {
  for (const rel of sources) {
    const source = read(rel);
    for (const [, name] of source.matchAll(/<mp-icon name="([a-z]+)"/g)) {
      assert.ok(TILE_ICONS.includes(name), `${rel} uses unknown icon ${name}`);
    }
    for (const [asset] of source.matchAll(/\/assets\/ui\/v4\/[\w-]+\.(svg|jpg|png)/g)) {
      assert.ok(fs.existsSync(path.join(root, asset)), `${rel} references missing ${asset}`);
    }
  }
  for (const name of TILE_ICONS) {
    assert.ok(fs.existsSync(path.join(root, `assets/ui/v4/${name}-accent.svg`)), name);
    assert.ok(fs.existsSync(path.join(root, `assets/ui/v4/${name}-ink.svg`)), name);
  }
});

test("pages register exactly the components their templates render", () => {
  for (const dir of pageDirs) {
    const wxml = read(`pages/${dir}/index.wxml`);
    const using = JSON.parse(read(`pages/${dir}/index.json`)).usingComponents || {};
    for (const name of Object.keys(using)) {
      assert.match(wxml, new RegExp(`<${name}[\\s>/]`), `${dir} registers unused ${name}`);
    }
  }
});
