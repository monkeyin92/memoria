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
  assert.deepEqual(
    companions.map((companion) => [companion.id, companion.voiceId, companion.voiceName]),
    [
      ["starlight", "warm_companion", "暖阳青年"],
      ["taoxi", "bright_peer", "元气搭子"],
      ["mianmian", "soft_confidante", "温柔知己"],
      ["axu", "calm_guide", "沉稳向导"],
      ["xuanmo", "low_magnetic", "低音笃定"],
    ],
  );
  assert.equal(companionById(defaultCompanionId).id, defaultCompanionId);
  assert.equal(companionById("unknown").id, defaultCompanionId);
  for (const companion of companions) {
    assert.equal(companion.face, undefined);
    assert.equal(companion.chest, undefined);
  }
});
