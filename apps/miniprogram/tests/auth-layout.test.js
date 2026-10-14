const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const authTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxml"),
  "utf8",
);
const authStyles = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxss"),
  "utf8",
);

test("auth header puts the draft chip and the two-line title on separate lines", () => {
  assert.match(
    authTemplate,
    /<view class="auth-copy">[\s\S]*?<text class="pill pill-draft auth-chip">草稿 · 待律师审核<\/text>[\s\S]*?<text class="page-title auth-copy-line">开始前，<\/text>\s*<text class="page-title auth-copy-line">请先确认这几件事<\/text>/,
  );
  assert.match(
    authStyles,
    /\.auth-copy-line\s*\{[\s\S]*?display:\s*block\s*;/,
  );
});

test("auth facts state only what the product does and promise no view or delete", () => {
  const facts = [...authTemplate.matchAll(/<text class="auth-fact-title">([^<]+)<\/text>/g)].map((m) => m[1]);
  assert.deepEqual(facts, ["对话经过我们的服务器", "保存对话摘要", "长期只记三项", "回应由 AI 生成"]);
  assert.match(authTemplate, /孩子的称呼、喜好、宠物名/);
  assert.doesNotMatch(authTemplate, /摄像头|随时查看|删除可证明|device-screen/);
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
