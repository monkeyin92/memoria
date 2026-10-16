const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");

const consentTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/child-consent/index.wxml"),
  "utf8",
);
const consentStyles = fs.readFileSync(
  path.join(__dirname, "../pages/child-consent/index.wxss"),
  "utf8",
);
const authTemplate = fs.readFileSync(
  path.join(__dirname, "../pages/auth/index.wxml"),
  "utf8",
);

test("child consent header puts the draft chip and the two-line title on separate lines", () => {
  assert.match(
    consentTemplate,
    /<view class="auth-copy">[\s\S]*?<text class="pill pill-teal auth-chip">\{\{releaseChip\}\}<\/text>[\s\S]*?<text class="page-title auth-copy-line">开始前，<\/text>\s*<text class="page-title auth-copy-line">请先确认这几件事<\/text>/,
  );
  assert.match(consentStyles, /\.auth-copy-line\s*\{[\s\S]*?display:\s*block\s*;/);
});

test("child consent facts state only what the product does", () => {
  const harness = loadPage("pages/child-consent/index");
  try {
    assert.deepEqual(
      harness.page.data.facts.map((fact) => fact.title),
      ["对话经过我们的服务器", "保存对话摘要", "长期只存三项", "回应由 AI 生成", "设备没有摄像头"],
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
  } finally {
    harness.restore();
  }
  assert.doesNotMatch(consentTemplate, /随时查看|删除可证明|device-screen|未来|连接器|后续|预留|将配备|下一代/);
});

test("the login page collects no avatar or nickname", () => {
  assert.doesNotMatch(authTemplate, /chooseAvatar|type="nickname"|怎么称呼你/);
  assert.match(authTemplate, /我已阅读并同意<text class="legal-link" catchtap="openUserAgreement">《用户服务协议》<\/text>和<text class="legal-link" catchtap="openPrivacyPolicy">《隐私政策》<\/text>/);
});
