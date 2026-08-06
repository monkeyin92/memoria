import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const configSource = readFileSync("vite.config.mjs", "utf8");

describe("local control API proxy", () => {
  it("rewrites the auth refresh cookie to the browser-facing path", () => {
    expect(configSource).toMatch(/cookiePathRewrite:\s*\{\s*"\/v1\/auth":\s*"\/memoria-api\/v1\/auth"/s);
  });
});
