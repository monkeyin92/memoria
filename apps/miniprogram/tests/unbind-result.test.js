const assert = require("node:assert/strict");
const test = require("node:test");

const { unbindResultLines } = require("../utils/unbind-result");

test("unbind keeping the data reports it without claiming any consent was withdrawn", () => {
  assert.deepEqual(
    unbindResultLines({
      child_layers_purged: "not_requested",
      subject_deletion: "not_requested",
      consents_withdrawn: 2,
    }),
    ["TA 的数据已保留，重新绑定时可以选回 TA"],
  );
  assert.deepEqual(
    unbindResultLines({
      child_layers_purged: "not_requested",
      subject_deletion: "not_requested",
      consents_withdrawn: 0,
    }),
    ["TA 的数据已保留，重新绑定时可以选回 TA"],
  );
});

test("the delete path still reports the consents it withdrew", () => {
  assert.deepEqual(
    unbindResultLines({
      child_layers_purged: "completed",
      subject_deletion: "completed",
      consents_withdrawn: 1,
    }),
    ["TA 的近期摘要和长期记录已删除", "已撤回 1 项授权。"],
  );
});

test("a completed delete says the summaries and long-term records are gone", () => {
  assert.deepEqual(
    unbindResultLines({
      child_layers_purged: "completed",
      subject_deletion: "completed",
      consents_withdrawn: 0,
    }),
    ["TA 的近期摘要和长期记录已删除"],
  );
});

test("an incomplete delete says it is still running, never that it finished", () => {
  assert.deepEqual(
    unbindResultLines({ child_layers_purged: "pending", subject_deletion: "pending" }),
    ["TA 的数据正在删除中，完成后不会再显示"],
  );
  assert.deepEqual(
    unbindResultLines({ child_layers_purged: "completed", subject_deletion: "pending" }),
    ["TA 的数据正在删除中，完成后不会再显示"],
  );
});

test("not applicable, not requested, missing or unknown values add no line", () => {
  assert.deepEqual(
    unbindResultLines({ child_layers_purged: "not_applicable", subject_deletion: "not_requested" }),
    [],
  );
  assert.deepEqual(unbindResultLines({ child_layers_purged: "weird", consents_withdrawn: "3" }), []);
  assert.deepEqual(unbindResultLines(null), []);
});

test("the result copy never uses internal deletion terms or the audit sentence", () => {
  const results = [
    { child_layers_purged: "not_requested", subject_deletion: "not_requested", consents_withdrawn: 1 },
    { child_layers_purged: "completed", subject_deletion: "completed" },
    { child_layers_purged: "pending", subject_deletion: "pending" },
  ];
  for (const result of results) {
    const text = unbindResultLines(result).join("\n");
    for (const forbidden of ["按使用人删除", "使用人", "运行"]) {
      assert.ok(!text.includes(forbidden), forbidden);
    }
    assert.ok(!text.includes("删除操作本身的记录"), "audit sentence is held back");
  }
});
