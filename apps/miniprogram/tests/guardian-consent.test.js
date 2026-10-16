const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");

const root = path.join(__dirname, "..");
const repo = path.join(root, "../..");
const FUTURE_HARDWARE = /未来|后续|以后|连接器|预留|将配备|下一代|新硬件/;

function visibleText(html) {
  return html.replace(/<head>[\s\S]*?<\/head>/, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function bodyParagraphs(markdown) {
  const marker = "## 正文\n\n";
  const start = markdown.indexOf(marker);
  assert.notEqual(start, -1);
  const rest = markdown.slice(start + marker.length);
  const end = rest.search(/\n## /);
  assert.notEqual(end, -1);
  return rest.slice(0, end).trim().split("\n\n");
}

test("guardian consent renders the draft chip, the source note, and the compliance paragraphs in order", () => {
  const markdown = fs.readFileSync(path.join(repo, "docs/compliance/guardian-consent-draft.md"), "utf8");
  const paragraphs = bodyParagraphs(markdown);
  const pageParagraphs = require("../pages/guardian-consent/paragraphs");
  assert.deepEqual(pageParagraphs, paragraphs);

  const harness = loadPage("pages/guardian-consent/index");
  try {
    const html = renderPage("pages/guardian-consent/index", harness.page.data);
    const text = visibleText(html);
    assert.match(text, /^内测版 监护人同意书 /);
    assert.doesNotMatch(text, /草稿/);
    assert.match(text, /来源：docs\/compliance\/child-audio-transcript-ephemeral\.md$/);
    let cursor = 0;
    for (const paragraph of paragraphs) {
      const at = text.indexOf(paragraph, cursor);
      assert.notEqual(at, -1, paragraph);
      cursor = at + paragraph.length;
    }
    assert.equal(text.includes("设备没有摄像头，也不采集图像。"), true);
    assert.equal((text.match(/设备没有摄像头，也不采集图像/g) || []).length, 1);
    assert.doesNotMatch(text, FUTURE_HARDWARE);
    assert.match(
      fs.readFileSync(path.join(root, "pages/guardian-consent/index.wxss"), "utf8"),
      /\.guardian-consent-p\s*\{[^}]*text-align:\s*left/,
    );
    const appConfig = require("../app.json");
    assert.equal(appConfig.pages.includes("pages/guardian-consent/index"), true);
  } finally {
    harness.restore();
  }
});
