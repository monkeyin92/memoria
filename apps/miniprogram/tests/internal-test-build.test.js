const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { spawnSync } = require("node:child_process");
const { withMiniprogramBuild } = require("../../../scripts/miniprogram_test_build");
const source = path.resolve(__dirname, "../utils/legal-release.js");

test("internal test builds inject consent only in an isolated copy and clean it", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "memoria-build-test-"));
  fs.mkdirSync(path.join(root, "utils"));
  fs.copyFileSync(source, path.join(root, "utils/legal-release.js"));
  fs.writeFileSync(path.join(root, ".env"), "must-not-copy");
  const before = fs.readFileSync(source, "utf8");
  let artifact;
  try {
    for (const unlock of [false, true]) {
      await withMiniprogramBuild(root, unlock, async (build) => {
        artifact = build;
        assert.notEqual(build, root);
        assert.equal(fs.existsSync(path.join(build, ".env")), false);
        const legal = require(path.join(build, "utils/legal-release.js"));
        assert.equal(legal.INTERNAL_TEST_CONSENT_UNLOCK, unlock);
        assert.equal(legal.consentLocked(true), !unlock);
        const gate = spawnSync(process.execPath, [path.resolve(__dirname, "../scripts/check-release-gate.js"), path.join(build, "utils/legal-release.js")]);
        assert.equal(gate.status, unlock ? 1 : 0);
      });
      assert.equal(fs.existsSync(artifact), false);
    }
    await assert.rejects(withMiniprogramBuild(root, true, async (build) => {
      artifact = build;
      throw new Error("compile failed");
    }), /compile failed/);
    assert.equal(fs.existsSync(artifact), false);
    assert.equal(fs.readFileSync(source, "utf8"), before);
    fs.writeFileSync(path.join(root, "utils/legal-release.js"), before.replace("UNLOCK = false", "UNLOCK = true"));
    await assert.rejects(withMiniprogramBuild(root, true, async () => assert.fail("unsafe source accepted")), /false/);
  } finally {
    fs.rmSync(root, { force: true, recursive: true });
  }
});
