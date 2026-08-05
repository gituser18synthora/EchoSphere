/* Shared chat clock/formatter. Both live Testing and stored Conversation
   Review use this module so the same turn always has the same MM:SS.xx label. */

const clockOriginMs = Date.now();
const performanceOriginMs = typeof performance === "undefined" ? 0 : performance.now();
let lastTimestampMicros = 0n;

export function nowWithMicroseconds(): string {
  const elapsedMs = typeof performance === "undefined"
    ? Date.now() - clockOriginMs
    : performance.now() - performanceOriginMs;
  let epochMicros = BigInt(clockOriginMs) * 1_000n
    + BigInt(Math.max(0, Math.floor(elapsedMs * 1_000)));
  if (epochMicros <= lastTimestampMicros) epochMicros = lastTimestampMicros + 1n;
  lastTimestampMicros = epochMicros;

  const iso = new Date(Number(epochMicros / 1_000n)).toISOString();
  const fraction = (epochMicros % 1_000_000n).toString().padStart(6, "0");
  return `${iso.slice(0, 19)}.${fraction}Z`;
}

export function formatChatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const fraction = (value.match(/\.(\d+)/)?.[1]
    ?? String(date.getMilliseconds()).padStart(3, "0"))
    .padEnd(2, "0")
    .slice(0, 2);
  const time = date.toLocaleTimeString("en-GB", {
    minute: "2-digit", second: "2-digit",
  });
  return `${time}.${fraction}`;
}
