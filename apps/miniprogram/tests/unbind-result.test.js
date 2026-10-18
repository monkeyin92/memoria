const assert = require("node:assert/strict");
const test = require("node:test");

const { unbindResultLines, unbindClearsChildLayers } = require("../utils/unbind-result");

test("completed child layers and deletion are reported as done", () => {
  assert.deepEqual(
    unbindResultLines({
      child_layers_purged: "completed",
      subject_deletion: "completed",
      consents_withdrawn: 1,
    }),
    ["TA 的近期摘要和长期记录已处理。", "按使用人删除已完成。", "已撤回 1 项授权。"],
  );
});

test("pending is shown to the guardian, never reported as done", () => {
  const lines = unbindResultLines({ child_layers_purged: "pending", subject_deletion: "pending" });
  assert.deepEqual(lines, [
    "TA 的近期摘要和长期记录仍在处理中，请稍候。",
    "按使用人删除仍在进行中。",
  ]);
});

test("not applicable, not requested, missing or unknown values add no line", () => {
  assert.deepEqual(
    unbindResultLines({ child_layers_purged: "not_applicable", subject_deletion: "not_requested", consents_withdrawn: 0 }),
    [],
  );
  assert.deepEqual(unbindResultLines({ child_layers_purged: "weird", consents_withdrawn: "3" }), []);
  assert.deepEqual(unbindResultLines(null), []);
});

test("only self-use bindings skip the child layer purge note", () => {
  assert.equal(unbindClearsChildLayers("self_use"), false);
  assert.equal(unbindClearsChildLayers("parent_for_child"), true);
  assert.equal(unbindClearsChildLayers("child_for_parent"), true);
  assert.equal(unbindClearsChildLayers("family_shared"), true);
});
