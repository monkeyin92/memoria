const assert = require("node:assert/strict");
const test = require("node:test");

const {
  companionById,
  companions,
  defaultCompanionId,
} = require("../utils/companions");

test("the Mini Program keeps the five approved companion styles", () => {
  assert.deepEqual(
    companions.map((companion) => companion.id),
    ["starlight", "taoxi", "mianmian", "axu", "xuanmo"],
  );
  assert.equal(companionById(defaultCompanionId).id, defaultCompanionId);
  assert.equal(companionById("unknown").id, defaultCompanionId);
});
