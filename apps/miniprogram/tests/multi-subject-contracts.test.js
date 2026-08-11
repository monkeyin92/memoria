const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const generated = require("../generated/multi-subject-contracts");
const contracts = require("../utils/multi-subject-contracts");

const schemaPath = path.resolve(
  __dirname,
  "../../../packages/contracts/schemas/multi-subject/multi-subject.schema.json",
);
const schemaBytes = fs.readFileSync(schemaPath);
const schema = JSON.parse(schemaBytes.toString("utf8"));

test("mini program consumes the generated canonical artifact through a thin re-export", () => {
  assert.strictEqual(contracts, generated);
  assert.equal(generated.CONTRACTS_SCHEMA_VERSION, schema.properties.schema_version.const);
  assert.equal(generated.OBJECT_CONTRACTS_VERSION, schema.objects.properties.objects_version.const);
  assert.equal(
    generated.SOURCE_HASH,
    crypto.createHash("sha256").update(schemaBytes).digest("hex"),
  );
});

test("every mini program enum exactly matches the canonical schema and fails closed", () => {
  assert.deepEqual(Object.keys(generated.ENUM_REGISTRY), Object.keys(schema.$defs));

  for (const [name, definition] of Object.entries(schema.$defs)) {
    const contract = generated.ENUM_REGISTRY[name];
    const metadata = definition["x-memoria"];

    assert.ok(contract, `${name} 必须存在于生成 registry`);
    assert.deepEqual(contract.values, definition.enum, `${name} 取值漂移`);
    assert.equal(contract.default, metadata.default ?? null, `${name} 默认值漂移`);
    assert.equal(contract.contract_version, metadata.contract_version, `${name} 版本漂移`);
    assert.equal(contract.missing_value_policy, "fail_closed", `${name} 必须 fail closed`);
    assert.equal(contract.guard(definition.enum[0]), true, `${name} canonical 值应通过`);
    assert.equal(contract.guard("not-a-canonical-value"), false, `${name} 未知值应拒绝`);
    assert.equal(contract.guard(undefined), false, `${name} 缺失值应拒绝`);
    assert.equal(contract.guard(null), false, `${name} null 应拒绝`);
  }
});

test("memory authority capabilities are distinct canonical actions", () => {
  for (const capability of [
    "memory_promotion",
    "family_shared_memory_proposal",
    "family_shared_memory_approval",
    "family_shared_memory_promotion",
  ]) {
    assert.equal(generated.isCapability(capability), true, capability);
    assert.equal(generated.CAPABILITY_VALUES.includes(capability), true, capability);
  }
  assert.equal(generated.isCapability("family_shared_memory_confirm"), false);
});

test("new producer lifecycle guard permits only canonical current contracts", () => {
  for (const legacy of ["RuntimeProfile", "RuntimeProfileSigned", "PolicyReceipt"]) {
    assert.equal(generated.CONTRACT_LIFECYCLE[legacy].consumer_mode, "migration_only");
    assert.equal(generated.CONTRACT_LIFECYCLE[legacy].new_producer, "forbidden");
    assert.equal(generated.isNewProducerContract(legacy), false);
    assert.throws(() => generated.requireNewProducerContract(legacy), /forbidden for new producers/);
  }

  for (const current of ["RuntimeProfileV2", "RuntimeProfileSignedV2", "PolicyReceiptV2"]) {
    assert.equal(generated.CONTRACT_LIFECYCLE[current].consumer_mode, "canonical");
    assert.equal(generated.CONTRACT_LIFECYCLE[current].new_producer, "allowed");
    assert.equal(generated.isNewProducerContract(current), true);
    assert.doesNotThrow(() => generated.requireNewProducerContract(current));
  }

  assert.equal(generated.isNewProducerContract("UnknownContract"), false);
  assert.throws(
    () => generated.requireNewProducerContract("UnknownContract"),
    /forbidden for new producers/,
  );
});
