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
    ["TA 的近期摘要和长期记录已清除。", "TA 的记忆和对话数据已删除。", "已撤回 1 项授权。"],
  );
});

test("pending is shown to the guardian, never reported as done", () => {
  const lines = unbindResultLines({ child_layers_purged: "pending", subject_deletion: "pending" });
  assert.deepEqual(lines, [
    "TA 的近期摘要和长期记录没有全部清除，请不要当作已清除。",
    "TA 的记忆和对话数据正在删除，后台会继续完成。",
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
