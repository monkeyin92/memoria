const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "..");
const childMemory = require("../utils/child-memory");

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

const PROFILE_WXML = "pages/profile/index.wxml";
const GUARDIAN_WXML = "pages/guardian/index.wxml";

const ABSOLUTE_ACCOUNT =
  /注销将永久删除账号及全部数据，且不可恢复/;
const ABSOLUTE_GUARDIAN =
  /将永久删除\s*TA\s*的记忆、对话和成长记录，且不可恢复/;
const ABSOLUTE_CHILD_MEMORY = /永久.*不可恢复|无法恢复/;

const STAGED_ACCOUNT =
  /提交后账号将停用，在线数据按阶段清理。完成前可能显示「处理中」。备份与外部处理方中的副本不在提交瞬间一并消失，按其各自策略清除。不要理解为全部副本已不可恢复/;
const STAGED_GUARDIAN =
  /将按阶段删除\s*TA\s*的在线记忆、对话和成长记录。完成前可能显示「处理中」。备份与外部处理方中的副本按其各自策略清除。不要理解为全部副本已不可恢复/;

test("profile delete modal keeps staged online-deletion saga wording", () => {
  const wxml = read(PROFILE_WXML);
  assert.doesNotMatch(wxml, ABSOLUTE_ACCOUNT);
  assert.match(wxml, STAGED_ACCOUNT);
});

test("guardian minor-delete modal keeps staged online-deletion saga wording", () => {
  const wxml = read(GUARDIAN_WXML);
  assert.doesNotMatch(wxml, ABSOLUTE_GUARDIAN);
  assert.match(wxml, STAGED_GUARDIAN);
});

test("child-memory delete/clear copy does not claim absolute irreversibility", () => {
  assert.doesNotMatch(childMemory.DELETE_CONFIRM, ABSOLUTE_CHILD_MEMORY);
  assert.doesNotMatch(childMemory.CLEAR_CONFIRM, ABSOLUTE_CHILD_MEMORY);
  assert.doesNotMatch(childMemory.CLEAR_CONFIRM, /不影响其他记忆/);
  assert.match(childMemory.DELETE_CONFIRM, /在线记忆|服务器/);
  assert.match(childMemory.CLEAR_CONFIRM, /在线记忆|服务器/);
  assert.match(childMemory.DELETE_CONFIRM, /备份/);
  assert.match(childMemory.CLEAR_CONFIRM, /备份/);
});
