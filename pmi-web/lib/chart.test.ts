import { describe, expect, it } from "vitest";

import { fittedDomain } from "./chart";

describe("fittedDomain", () => {
  it("pads around the data", () => {
    expect(fittedDomain([8.2, 8.8])).toEqual([7, 10]);
  });

  it("pads a 25% margin on wider ranges", () => {
    expect(fittedDomain([40, 60])).toEqual([35, 65]);
  });

  it("clamps to the index range", () => {
    expect(fittedDomain([0.5, 99.5])).toEqual([0, 100]);
  });

  it("falls back to the full range with no data", () => {
    expect(fittedDomain([])).toEqual([0, 100]);
  });
});
