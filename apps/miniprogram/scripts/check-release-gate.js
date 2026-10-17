#!/usr/bin/env node
const path = require("node:path");

const defaultFile = path.join(__dirname, "..", "utils", "legal-release.js");

function main() {
  const file = path.resolve(process.argv[2] || defaultFile);
  const resolved = require.resolve(file);
  delete require.cache[resolved];
  const legal = require(resolved);
  if (legal.INTERNAL_TEST_CONSENT_UNLOCK === true) {
    console.error(
      "INTERNAL_TEST_CONSENT_UNLOCK is true. An experience build must flip it locally and must not commit it.",
    );
    process.exit(1);
  }
  process.exit(0);
}

main();
