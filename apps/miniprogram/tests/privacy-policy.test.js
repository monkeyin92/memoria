const assert = require("node:assert/strict");
const test = require("node:test");

const { loadPage } = require("../scripts/mp-harness/runtime");
const { renderPage } = require("../scripts/mp-harness/render");

const paragraphs = require("../pages/privacy-policy/paragraphs");

// Paragraphs 1-7 are shipped text. A change here is a product decision, not a
// refactor, so the test pins them.
const SHIPPED = [
  "Memoria 是给 3 到 10 岁孩子的陪伴机器人。孩子在设备上使用，家长在手机上查看和管理。",
  "登录使用微信提供的登录凭证，以及你在登录页授权的手机号。未登录时可以阅读本页和《用户服务协议》，不会读取个人回顾或资料。",
  "在设备上可以陪孩子聊天，也可以听已经放在设备上的离线故事。",
  "家长可以设置每次和每天的时长上限。",
  "家长可以查看和删除聊天摘要。",
  "回应由 AI 生成，会明确标识。",
  "设备没有摄像头。手机不录音。",
];

// 8-12 are the compliance-approved text, pinned verbatim. 10's backup sentence
// and all of 12 are copy the ruling keeps without a code check, so they stay.
const APPROVED = [
  "孩子说的话会经我们的服务器，交给三家服务商处理：阿里云百炼把语音转成文字，并帮忙查找记忆；DeepSeek 生成回复、做安全判断和摘要；豆包把回复读出来。服务商保存多久，按我们和它们签的合同",
  "录音只用来当场回应，不保存。对话摘要保存 30 天。长期只记孩子的称呼、喜好、宠物名，180 天没用会自动清除",
  "选择「解绑，并删除 TA 的数据」后，我们会删除服务端保存的孩子相关记录，一般当场完成，遇到故障会自动重试；删除功能暂时不可用时不会解绑，页面会提示你。备份里的数据不再使用，会随备份轮换清除",
  "你可以在小程序「帮助与反馈」里写明「申请删除孩子的数据」，我们人工处理，15 个工作日内完成并回复你",
  "有问题或投诉，可以在小程序「帮助与反馈」里提，我们 2 个工作日内回复，15 个工作日内给出处理结果。负责处理的是「儿童个人信息保护负责人」",
];

function visibleText(html) {
  return html
    .replace(/<head>[\s\S]*?<\/head>/, "")
    .replace(/<style>[\s\S]*?<\/style>/, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

test("privacy policy keeps paragraphs 1-7 exactly as shipped", () => {
  assert.deepEqual(paragraphs.slice(0, 7), SHIPPED);
});

test("privacy policy keeps paragraphs 8-12 verbatim, in order, and stops at 12", () => {
  assert.deepEqual(paragraphs.slice(7), APPROVED);
  assert.equal(paragraphs.length, 12);
  for (const paragraph of paragraphs) {
    assert.equal(typeof paragraph, "string");
    assert.doesNotMatch(paragraph, /^\s*\d+[.、]\s*/, "no paragraph numbering in the array");
    assert.ok(paragraph.trim().length > 0);
  }
});

test("the privacy policy page renders the paragraphs in array order", async () => {
  const harness = loadPage("pages/privacy-policy/index");
  await harness.run({});
  try {
    const text = visibleText(renderPage("pages/privacy-policy/index", harness.page.data));
    assert.match(text, /^隐私政策 /);
    let cursor = 0;
    for (const paragraph of paragraphs) {
      const at = text.indexOf(paragraph, cursor);
      assert.notEqual(at, -1, paragraph);
      cursor = at + paragraph.length;
    }
  } finally {
    harness.restore();
  }
});
