const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");

const authTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxml"),
  "utf8",
);
const authStyles = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxss"),
  "utf8",
);
const authScript = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.js"),
  "utf8",
);

test("auth header puts the draft chip and the two-line title on separate lines", () => {
  assert.match(
    authTemplate,
    /<view class="auth-copy">[\s\S]*?<text class="pill pill-teal auth-chip">\{\{releaseChip\}\}<\/text>[\s\S]*?<text class="page-title auth-copy-line">开始前，<\/text>\s*<text class="page-title auth-copy-line">请先确认这几件事<\/text>/,
  );
  assert.match(
    authStyles,
    /\.auth-copy-line\s*\{[\s\S]*?display:\s*block\s*;/,
  );
});

test("auth facts state only what the product does and promise no view or delete", () => {
  const harness = loadPage("pages/auth/index");
  try {
    assert.deepEqual(
      harness.page.data.facts.map((fact) => fact.title),
      [
        "对话经过我们的服务器",
        "保存对话摘要",
        "长期只存三项",
        "回应由 AI 生成",
        "设备没有摄像头",
      ],
    );
    assert.deepEqual(
      harness.page.data.facts.map((fact) => fact.subtitle),
      [
        "孩子说的话会经服务器处理后再回复",
        "原话和录音只用来当场回应，不保存",
        "孩子的称呼、喜好、宠物名",
        "会明确标识；模型训练默认关闭",
        "不拍照，也不录像",
      ],
    );
    assert.deepEqual(
      harness.page.data.facts.map((fact) => fact.icon),
      ["server", "note", "star", "info", "cam"],
    );
  } finally {
    harness.restore();
  }
  assert.match(authTemplate, /<mp-icon name="\{\{item.icon\}\}" tone="ink"><\/mp-icon>/);
  assert.doesNotMatch(authTemplate + authScript, /随时查看|删除可证明|device-screen|未来|连接器|后续|预留|将配备|下一代/);
});

test("auth header has no unselected mascot placeholder or redundant subtitle", () => {
  assert.doesNotMatch(authTemplate, /brand-stage|brand-mascot|page-subtitle/);
  assert.doesNotMatch(authStyles, /\.brand-(?:stage|halo|ring|spark|mascot)\b/);
});

test("first registration asks how to address the user and explains the effect", () => {
  assert.match(
    authTemplate,
    /怎么称呼你？\{\{needsSalutation \? ' \*' : ''\}\}[\s\S]*placeholder="例如：朋友、主人、小明"/,
  );
  assert.match(authTemplate, /首次注册后，我会在自然的对话里这样称呼你。/);
});
