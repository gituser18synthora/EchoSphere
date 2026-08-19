/* Shared monetary formatting + display-currency conversion.

   The platform base currency is USD; the backend computes authoritative
   costs and conversions. This module only renders amounts and applies the
   backend-provided USD→X rates for live display — the stored base cost
   never changes with the selected display currency. */

export interface CurrencyInfo {
  code: string;
  name: string;
  symbol: string;
  decimalPlaces: number;
  isBase: boolean;
  /** Usable for display: it's the base currency or has a configured rate. */
  hasRate: boolean;
}

export interface CurrencyRates {
  baseCurrency: string;
  currencies: CurrencyInfo[];
  /** USD → code rates currently in force (base currency omitted). */
  rates: Record<string, number>;
}

export const BASE_CURRENCY = "USD";

/* Locale drives digit grouping: INR uses the Indian lakh/crore system
   (₹1,23,456.78); everything else uses western grouping. */
const CURRENCY_LOCALES: Record<string, string> = { INR: "en-IN" };

export function formatMoney(
  amount: number,
  code: string,
  opts: { precise?: boolean } = {},
): string {
  /* `precise` keeps very small per-request costs meaningful ($0.0009)
     without rendering long tails on dashboard totals. */
  const maximumFractionDigits =
    opts.precise && Math.abs(amount) < 1 ? 4 : 2;
  try {
    return new Intl.NumberFormat(CURRENCY_LOCALES[code] ?? "en-US", {
      style: "currency",
      currency: code,
      minimumFractionDigits: 2,
      maximumFractionDigits,
    }).format(amount);
  } catch {
    // Unknown/legacy code — never crash a dashboard over formatting.
    return `${amount.toFixed(2)} ${code}`;
  }
}

/** Convert a USD amount for display; null when no rate is configured. */
export function convertUsd(
  amountUsd: number,
  code: string,
  rates: Record<string, number> | undefined,
): number | null {
  if (code === BASE_CURRENCY) return amountUsd;
  const rate = rates?.[code];
  return typeof rate === "number" && rate > 0 ? amountUsd * rate : null;
}

/** `$12.45 / ₹1,076.93` — base amount with the converted display amount. */
export function formatDualMoney(
  amountUsd: number,
  code: string,
  rates: Record<string, number> | undefined,
  opts: { precise?: boolean } = {},
): string {
  const base = formatMoney(amountUsd, BASE_CURRENCY, opts);
  if (code === BASE_CURRENCY) return base;
  const converted = convertUsd(amountUsd, code, rates);
  return converted === null ? base : `${base} / ${formatMoney(converted, code, opts)}`;
}

/* Persisted display-currency preference (UI-only; never sent to the API). */
const DISPLAY_CURRENCY_KEY = "echosphere.displayCurrency";

export function getStoredDisplayCurrency(): string {
  try {
    return localStorage.getItem(DISPLAY_CURRENCY_KEY) || BASE_CURRENCY;
  } catch {
    return BASE_CURRENCY;
  }
}

export function storeDisplayCurrency(code: string): void {
  try {
    localStorage.setItem(DISPLAY_CURRENCY_KEY, code);
  } catch {
    /* storage unavailable — the selection just won't persist */
  }
}

/** Whether a KPI/metric label is financial. Defense-in-depth for viewers
    without the costs.view permission: the backend already omits these, but
    the UI must never render one even if a payload or cached session drifts. */
export function isCostLabel(label: string): boolean {
  return /cost|price|pricing|spend|billing|revenue|mrr/i.test(label);
}
