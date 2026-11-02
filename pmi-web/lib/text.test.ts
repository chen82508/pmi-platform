import { describe, expect, it } from "vitest";

import { formatVolume } from "./text";

describe("formatVolume", () => {
  it("treats its input as dollars, not thousands (regression: a formatter once assumed thousands)", () => {
    // Inputs are raw dollar volumes; the comments show what the buggy
    // thousands-based formatter used to print.
    expect(formatVolume(5891.336)).toBe("$5.9k"); // page printed "$5.9M"
    expect(formatVolume(267.6309)).toBe("$268"); // page printed "$268K"
    expect(formatVolume(82948)).toBe("$82.9k"); // page printed "$82.9M"
    expect(formatVolume(206973.8611)).toBe("$207.0k"); // page printed "$207.1M"
  });

  it("promotes to the next suffix just under each tier", () => {
    expect(formatVolume(999.4)).toBe("$999");
    expect(formatVolume(999.5)).toBe("$1.0k");
    expect(formatVolume(999_949)).toBe("$999.9k");
    expect(formatVolume(999_950)).toBe("$1.0M");
    expect(formatVolume(1_250_000)).toBe("$1.3M");
    expect(formatVolume(2_000_000_000)).toBe("$2.0B");
  });

  it("distinguishes a real zero from an unknown volume", () => {
    expect(formatVolume(0)).toBe("$0");
    expect(formatVolume(null)).toBeNull();
    expect(formatVolume(undefined)).toBeNull();
    expect(formatVolume(-1)).toBeNull();
    expect(formatVolume(Number.NaN)).toBeNull();
  });
});
