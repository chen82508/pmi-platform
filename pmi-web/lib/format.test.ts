import { describe, expect, it } from "vitest";

import { seriesChange, unitOf } from "./format";

const pt = (as_of: string, score: number | null) => ({ as_of, score, component_count: 1 });

describe("unitOf", () => {
  it("labels seat projections in seats", () => {
    expect(unitOf("us-senate-2026-republican-seats")).toBe("seats");
    expect(unitOf("war-index-2026")).toBe("/ 100");
  });
});

describe("seriesChange", () => {
  it("measures first to last scored point, skipping gaps", () => {
    const out = seriesChange([
      pt("2026-10-19T00:00:00Z", null),
      pt("2026-10-20T00:00:00Z", 50),
      pt("2026-10-27T00:00:00Z", null),
      pt("2026-11-03T00:00:00Z", 52.5),
    ]);
    expect(out).toEqual({ delta: 2.5, days: 14 });
  });

  it("needs two scored points", () => {
    expect(seriesChange([pt("2026-10-20T00:00:00Z", 50)])).toBeNull();
    expect(seriesChange([])).toBeNull();
  });
});
