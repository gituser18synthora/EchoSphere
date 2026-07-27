import { describe, expect, it } from "vitest";
import {
  BASE_CURRENCY,
  convertUsd,
  formatDualMoney,
  formatMoney,
  getStoredDisplayCurrency,
  storeDisplayCurrency,
} from "./money";

describe("formatMoney", () => {
  it("formats USD with western grouping", () => {
    expect(formatMoney(1234.56, "USD")).toBe("$1,234.56");
  });

  it("formats INR with lakh/crore grouping", () => {
    expect(formatMoney(123456.78, "INR")).toBe("₹1,23,456.78");
  });

  it("formats EUR and GBP", () => {
    expect(formatMoney(1234.56, "EUR")).toBe("€1,234.56");
    expect(formatMoney(1234.56, "GBP")).toBe("£1,234.56");
  });

  it("keeps small per-request costs meaningful in precise mode", () => {
    expect(formatMoney(0.0009, "USD", { precise: true })).toBe("$0.0009");
    // Precise mode never lengthens large totals.
    expect(formatMoney(1076.925, "USD", { precise: true })).toBe("$1,076.93");
  });

  it("never crashes on an unknown code", () => {
    expect(formatMoney(5, "???")).toContain("5.00");
  });
});

describe("convertUsd", () => {
  const rates = { INR: 86.5, EUR: 0.92 };

  it("applies the configured USD rate", () => {
    expect(convertUsd(12.45, "INR", rates)).toBeCloseTo(1076.925, 6);
    expect(convertUsd(100, "EUR", rates)).toBeCloseTo(92, 6);
  });

  it("returns the amount unchanged for the base currency", () => {
    expect(convertUsd(12.45, BASE_CURRENCY, rates)).toBe(12.45);
  });

  it("returns null when no rate is configured", () => {
    expect(convertUsd(12.45, "GBP", rates)).toBeNull();
    expect(convertUsd(12.45, "INR", undefined)).toBeNull();
  });
});

describe("formatDualMoney", () => {
  const rates = { INR: 86.5 };

  it("shows base and converted amounts together", () => {
    expect(formatDualMoney(12.45, "INR", rates)).toBe("$12.45 / ₹1,076.93");
  });

  it("shows only the base amount when USD is selected", () => {
    expect(formatDualMoney(12.45, "USD", rates)).toBe("$12.45");
  });

  it("falls back to base-only when the rate is missing", () => {
    expect(formatDualMoney(12.45, "GBP", rates)).toBe("$12.45");
  });
});

describe("display currency preference", () => {
  it("defaults to USD and persists the selection", () => {
    expect(getStoredDisplayCurrency()).toBe("USD");
    storeDisplayCurrency("INR");
    expect(getStoredDisplayCurrency()).toBe("INR");
  });
});
