#!/usr/bin/env node
/*
 * Writes the v4 line icons (docs/design/app-v4/app.html) as colored SVG files.
 * WeChat renders SVG through <image>, not inline <svg> or rich-text, and SVG
 * files cannot read CSS currentColor, so each color variant is its own file.
 * Rerun after editing ICONS or VARIANTS: node scripts/build-v4-icons.js
 */
const fs = require("node:fs");
const path = require("node:path");
const { TILE_ICONS } = require("../components/mp-icon/tile-icons");

const OUT_DIR = path.join(__dirname, "..", "assets", "ui", "v4");

const ICONS = {
  home: '<path d="M3.5 10.5 12 4l8.5 6.5V19a1.5 1.5 0 0 1-1.5 1.5h-4v-6h-6v6H5A1.5 1.5 0 0 1 3.5 19z"/>',
  book: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"/>',
  face: '<rect x="4" y="6" width="16" height="13" rx="5"/><path d="M12 6V3.5"/><circle cx="12" cy="3" r=".6"/><path d="M9 12v1M15 12v1M10 16c1.2.8 2.8.8 4 0"/>',
  dev: '<rect x="5" y="3.5" width="14" height="17" rx="4"/><circle cx="12" cy="10" r="3"/><path d="M9.5 16.5h5"/>',
  user: '<circle cx="12" cy="8.5" r="4"/><path d="M4.5 20c1.3-3.6 4.2-5.5 7.5-5.5s6.2 1.9 7.5 5.5"/>',
  heart: '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>',
  note: '<path d="M7 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9.5A1.5 1.5 0 0 1 5.5 19V5A1.5 1.5 0 0 1 7 3.5z"/><path d="M9 12h6M9 15.5h4"/>',
  server: '<rect x="4" y="4" width="16" height="7" rx="2"/><rect x="4" y="13" width="16" height="7" rx="2"/><path d="M8 7.5h.01M8 16.5h.01"/>',
  star: '<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.3L12 16.3l-4.8 2.5.9-5.3L4.2 9.7l5.4-.8z"/>',
  cam: '<path d="M3 3l18 18"/><path d="M9 5h5l1.5 2H19a1.5 1.5 0 0 1 1.5 1.5V16M17 19H5a1.5 1.5 0 0 1-1.5-1.5v-9A1.5 1.5 0 0 1 5 7h1"/><path d="M10 10.5a3 3 0 0 0 4 4"/>',
  tag: '<path d="M3.5 12.2V5A1.5 1.5 0 0 1 5 3.5h7.2l8.3 8.3-8.7 8.7z"/><circle cx="8" cy="8" r="1.3"/>',
  shield: '<path d="M12 3.5 19 6v5.5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.5a2.5 2.5 0 0 1 4.8.8c0 1.7-2.4 2-2.4 3.6M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
  moon: '<path d="M19 14.5A7.5 7.5 0 0 1 9.5 5a7.5 7.5 0 1 0 9.5 9.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  wechat: '<path d="M9.5 5C5.9 5 3 7.4 3 10.4c0 1.7.9 3.2 2.4 4.2L5 16.8l2.4-1.3c.7.2 1.4.3 2.1.3M21 14.2c0-2.6-2.5-4.7-5.6-4.7s-5.6 2.1-5.6 4.7 2.5 4.7 5.6 4.7c.6 0 1.2-.1 1.8-.3l2 1.1-.4-1.9c1.3-.9 2.2-2.2 2.2-3.6z"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  back: '<path d="m15 6-6 6 6 6"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  paw: '<circle cx="7" cy="9" r="1.8"/><circle cx="11" cy="6" r="1.8"/><circle cx="15.5" cy="7" r="1.8"/><circle cx="18" cy="11" r="1.6"/><path d="M8.5 16.5c0-2.5 2-4.5 4-4.5s4 2 4 4.5c0 1.6-1.3 2.5-2.6 2.2-1-.2-1.8-.2-2.8 0-1.3.3-2.6-.6-2.6-2.2z"/>',
  bluetooth: '<path d="m7 7.5 10 9-5 4V3.5l5 4-10 9"/>',
  chart: '<path d="M4.5 19.5h15"/><path d="M7.5 16v-4M12 16V7.5M16.5 16v-6"/>',
  export: '<path d="M12 15V4M8 7.5 12 4l4 3.5"/><path d="M5 12.5V18a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 18v-5.5"/>',
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M6 11.5a6 6 0 0 0 12 0M12 17.5v3"/>',
  pen: '<path d="M4.5 19.5 5.5 15 15.5 5a2.1 2.1 0 0 1 3 3l-10 10z"/><path d="m13.5 7 3 3"/>',
  qr: '<rect x="4" y="4" width="6" height="6" rx="1.5"/><rect x="14" y="4" width="6" height="6" rx="1.5"/><rect x="4" y="14" width="6" height="6" rx="1.5"/><path d="M14 14h2.5v2.5H14zM17.5 17.5H20V20h-2.5z"/>',
  volume: '<path d="M4.5 9.5h3l4.5-4v13l-4.5-4h-3z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  wave: '<path d="M3.5 12h2M7.5 8.5v7M11.5 5.5v13M15.5 8.5v7M19.5 11v2"/>',
  wifi: '<path d="M4 9.5a11.5 11.5 0 0 1 16 0M7 12.8a7.2 7.2 0 0 1 10 0M10 16a3 3 0 0 1 4 0"/><path d="M12 19.2h.01"/>',
  leaf: '<path d="M12 20v-7"/><path d="M12 13c0-4 3-6.5 7.5-6.5 0 4.5-3 6.5-7.5 6.5z"/><path d="M12 15.5C12 12.5 9.8 10.5 5 10.5c0 3.2 2.5 5 7 5z"/>',
};

const VARIANTS = {
  "tab-off": { stroke: "#8E91A3", fill: "none" },
  "tab-on": { stroke: "#4A52C9", fill: "#EEEFFC" },
  accent: { stroke: "#4A52C9", fill: "none" },
  strong: { stroke: "#16182B", fill: "none" },
  ink: { stroke: "#5B5F73", fill: "none" },
  faint: { stroke: "#B4B6C4", fill: "none" },
  rust: { stroke: "#9A4732", fill: "none" },
  white: { stroke: "#FFFFFF", fill: "none" },
};

const WANTED = [
  ...["home", "book", "face", "dev", "user"].flatMap((name) => [
    [name, "tab-off"],
    [name, "tab-on"],
  ]),
  ...TILE_ICONS.flatMap((name) => [
    [name, "accent"],
    [name, "ink"],
  ]),
  ["heart", "rust"],
  ["chev", "rust"],
  ["chev", "faint"],
  ["back", "strong"],
  ["plus", "accent"],
  ["wechat", "white"],
];

function svg(name, variant) {
  const { stroke, fill } = VARIANTS[variant];
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" ` +
    `stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>\n`
  );
}

function iconPath(name, variant) {
  return `/assets/ui/v4/${name}-${variant}.svg`;
}

function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  for (const [name, variant] of WANTED) {
    fs.writeFileSync(path.join(OUT_DIR, `${name}-${variant}.svg`), svg(name, variant));
  }
  console.log(`wrote ${WANTED.length} icons to ${path.relative(process.cwd(), OUT_DIR)}`);
}

if (require.main === module) main();

module.exports = { ICONS, TILE_ICONS, VARIANTS, WANTED, iconPath, svg };
