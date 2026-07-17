import { describe, expect, it } from "vitest";

import { localDateKey } from "./date.js";

describe("localDateKey", () => {
  it("uses the browser-local calendar date instead of the UTC date", () => {
    const localMorning = {
      getFullYear: () => 2026,
      getMonth: () => 6,
      getDate: () => 5,
      toISOString: () => "2026-07-04T16:30:00.000Z",
    };

    expect(localDateKey(localMorning)).toBe("2026-07-05");
  });
});
