import { describe, expect, it } from "vitest";

import { parseBacktestCsv } from "./backtest";

describe("parseBacktestCsv", () => {
  it("reads columns by header name and ignores extra ones", () => {
    const csv = [
      "as_of,score,component_count,markets_priced",
      "2026-10-20T00:00:00Z,7.84,10,6",
      "2026-10-21T00:00:00Z,8.01,10,6",
    ].join("\n");
    expect(parseBacktestCsv(csv)).toEqual([
      { as_of: "2026-10-20T00:00:00Z", score: 7.84, component_count: 10 },
      { as_of: "2026-10-21T00:00:00Z", score: 8.01, component_count: 10 },
    ]);
  });

  it("keeps an empty score as a gap rather than zero", () => {
    const csv = "as_of,score,component_count\n2026-10-20T00:00:00Z,,0\n";
    expect(parseBacktestCsv(csv)).toEqual([
      { as_of: "2026-10-20T00:00:00Z", score: null, component_count: 0 },
    ]);
  });

  it("returns no points without rows or without an as_of column", () => {
    expect(parseBacktestCsv("")).toEqual([]);
    expect(parseBacktestCsv("as_of,score,component_count")).toEqual([]);
    expect(parseBacktestCsv("score,component_count\n7.8,10")).toEqual([]);
  });
});
