/* Display-currency selection + dual-currency money rendering.

   The selector changes only how amounts are RENDERED — stored base costs
   (USD) never change. Rates and available currencies come from the backend
   (/currency/rates), which is the authority for conversions. */

import { useState } from "react";
import { getCurrencyRates } from "@/services/api";
import {
  BASE_CURRENCY,
  convertUsd,
  formatMoney,
  getStoredDisplayCurrency,
  storeDisplayCurrency,
  type CurrencyRates,
} from "@/services/money";
import { useAsync } from "@/hooks/useAsync";

export interface DisplayCurrencyState {
  /** Selected display currency (falls back to USD until rates load). */
  currency: string;
  setCurrency: (code: string) => void;
  rates: CurrencyRates | null;
  /** `$12.45` or `$12.45 / ₹1,076.93` depending on the selection. */
  dual: (amountUsd: number, opts?: { precise?: boolean }) => string;
  /** Amount rendered in the selected currency only. */
  display: (amountUsd: number, opts?: { precise?: boolean }) => string;
}

export function useDisplayCurrency(): DisplayCurrencyState {
  const { data } = useAsync<CurrencyRates>(() => getCurrencyRates());
  const [currency, setCurrencyState] = useState<string>(getStoredDisplayCurrency());

  const setCurrency = (code: string) => {
    setCurrencyState(code);
    storeDisplayCurrency(code);
  };

  const rates = data ?? null;
  const usable = currency === BASE_CURRENCY || Boolean(rates?.rates?.[currency]);
  const effective = usable ? currency : BASE_CURRENCY;

  const display = (amountUsd: number, opts: { precise?: boolean } = {}) => {
    const converted = convertUsd(amountUsd, effective, rates?.rates);
    return converted === null
      ? formatMoney(amountUsd, BASE_CURRENCY, opts)
      : formatMoney(converted, effective, opts);
  };

  const dual = (amountUsd: number, opts: { precise?: boolean } = {}) => {
    const base = formatMoney(amountUsd, BASE_CURRENCY, opts);
    if (effective === BASE_CURRENCY) return base;
    const converted = convertUsd(amountUsd, effective, rates?.rates);
    return converted === null ? base : `${base} / ${formatMoney(converted, effective, opts)}`;
  };

  return { currency: effective, setCurrency, rates, dual, display };
}

/** Compact display-currency selector; options come from the active currency
    catalog, disabled when a currency has no configured exchange rate. */
export function CurrencySelect({ state }: { state: DisplayCurrencyState }) {
  const currencies = state.rates?.currencies ?? [];
  return (
    <select
      className="select"
      aria-label="Display currency"
      value={state.currency}
      onChange={(e) => state.setCurrency(e.target.value)}
    >
      {currencies.length === 0 && <option value={BASE_CURRENCY}>USD</option>}
      {currencies.map((c) => (
        <option key={c.code} value={c.code} disabled={!c.hasRate}>
          {c.code} · {c.symbol}{c.hasRate ? "" : " (no rate)"}
        </option>
      ))}
    </select>
  );
}
