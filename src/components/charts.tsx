/* ============================================================
   Chart library — plain SVG, dataviz-method compliant:
   thin marks, 2px lines, 4px rounded bar ends anchored to the
   baseline, 2px surface gaps between fills, recessive grid,
   legend whenever ≥2 series, crosshair + tooltip hover layer.
   Series colors come from validated CSS custom properties
   (--series-1 … --series-8) — fixed order, never cycled.
   ============================================================ */

import {
  useLayoutEffect, useRef, useState, type ReactNode,
} from "react";

export const seriesColor = (i: number) => `var(--series-${(i % 8) + 1})`;

export function fmtNum(v: number): string {
  if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 10_000) return `${(v / 1000).toFixed(0)}K`;
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)}K`;
  return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1);
}

function useWidth(): [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(600);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) setW(width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

/* ---------- Legend ---------- */
export function Legend({ items, shape = "square" }: { items: { label: string; color: string }[]; shape?: "square" | "line" }) {
  return (
    <div className="viz-legend">
      {items.map((it) => (
        <span className="viz-legend-item" key={it.label}>
          <span className={`viz-legend-swatch${shape === "line" ? " line" : ""}`} style={{ background: it.color }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

/* ---------- Tooltip ---------- */
interface TipState {
  x: number;
  y: number;
  title: string;
  rows: { label: string; value: string; color?: string }[];
}
function Tip({ tip, width }: { tip: TipState; width: number }) {
  const flip = tip.x > width - 170;
  return (
    <div
      className="viz-tooltip"
      style={{ left: flip ? undefined : tip.x + 14, right: flip ? width - tip.x + 14 : undefined, top: Math.max(0, tip.y - 20) }}
    >
      <div className="viz-tooltip-title">{tip.title}</div>
      {tip.rows.map((r, i) => (
        <div className="viz-tooltip-row" key={i}>
          <span className="row gap-6">
            {r.color && <span className="swatch" style={{ background: r.color }} />}
            {r.label}
          </span>
          <span className="val">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

/* ---------- Sparkline ---------- */
export function Sparkline({ data, width = 80, height = 24, color = "var(--series-1)" }: {
  data: number[]; width?: number; height?: number; color?: string;
}) {
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * (width - 2) + 1;
    const y = height - 2 - ((v - min) / span) * (height - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg className="sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/* ---------- Line / area chart ---------- */
export interface LineSeries {
  key: string;
  label: string;
  color?: string;
  area?: boolean;
}
export function LineChart({ data, x, series, height = 220, yFmt = fmtNum, maxTicks = 8, unit }: {
  data: Record<string, string | number>[];
  x: string;
  series: LineSeries[];
  height?: number;
  yFmt?: (v: number) => string;
  maxTicks?: number;
  unit?: string;
}) {
  const [ref, width] = useWidth();
  const [tip, setTip] = useState<TipState | null>(null);
  const [hoverI, setHoverI] = useState<number | null>(null);

  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const iw = Math.max(40, width - pad.l - pad.r);
  const ih = height - pad.t - pad.b;

  const allVals = data.flatMap((d) => series.map((s) => Number(d[s.key]) || 0));
  const maxV = Math.max(1, ...allVals);
  const yMax = niceMax(maxV);
  const px = (i: number) => pad.l + (data.length < 2 ? iw / 2 : (i / (data.length - 1)) * iw);
  const py = (v: number) => pad.t + ih - (v / yMax) * ih;

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const labelEvery = Math.max(1, Math.ceil(data.length / maxTicks));

  const colors = series.map((s, i) => s.color ?? seriesColor(i));

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const frac = (mx - pad.l) / iw;
    const i = Math.round(frac * (data.length - 1));
    if (i < 0 || i > data.length - 1) { setTip(null); setHoverI(null); return; }
    setHoverI(i);
    setTip({
      x: px(i),
      y: pad.t + 10,
      title: String(data[i][x]),
      rows: series.map((s, si) => ({
        label: s.label,
        value: `${yFmt(Number(data[i][s.key]) || 0)}${unit ?? ""}`,
        color: colors[si],
      })),
    });
  };

  return (
    <div className="viz" ref={ref} style={{ height }}>
      <svg width={width} height={height} onMouseMove={onMove} onMouseLeave={() => { setTip(null); setHoverI(null); }}>
        {yTicks.map((v) => (
          <g key={v}>
            <line className={v === 0 ? "baseline" : "grid-line"} x1={pad.l} x2={pad.l + iw} y1={py(v)} y2={py(v)} />
            <text className="axis-tick" x={pad.l - 8} y={py(v) + 3.5} textAnchor="end">{yFmt(v)}</text>
          </g>
        ))}
        {data.map((d, i) =>
          i % labelEvery === 0 ? (
            <text key={i} x={px(i)} y={height - 8} textAnchor="middle">{String(d[x])}</text>
          ) : null,
        )}
        {series.map((s, si) => {
          const pts = data.map((d, i) => [px(i), py(Number(d[s.key]) || 0)] as const);
          const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
          return (
            <g key={s.key}>
              {s.area && (
                <path
                  d={`${line} L${px(data.length - 1)} ${py(0)} L${px(0)} ${py(0)} Z`}
                  fill={colors[si]}
                  opacity={0.09}
                />
              )}
              <path d={line} fill="none" stroke={colors[si]} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            </g>
          );
        })}
        {hoverI !== null && (
          <g>
            <line className="viz-crosshair" x1={px(hoverI)} x2={px(hoverI)} y1={pad.t} y2={pad.t + ih} />
            {series.map((s, si) => (
              <circle
                key={s.key}
                cx={px(hoverI)}
                cy={py(Number(data[hoverI][s.key]) || 0)}
                r={4}
                fill={colors[si]}
                stroke="var(--chart-surface)"
                strokeWidth={2}
              />
            ))}
          </g>
        )}
      </svg>
      {tip && <Tip tip={tip} width={width} />}
    </div>
  );
}

/* ---------- Bar chart (grouped or stacked) ---------- */
export function BarChart({ data, x, series, height = 220, stacked, yFmt = fmtNum, maxTicks = 10 }: {
  data: Record<string, string | number>[];
  x: string;
  series: LineSeries[];
  height?: number;
  stacked?: boolean;
  yFmt?: (v: number) => string;
  maxTicks?: number;
}) {
  const [ref, width] = useWidth();
  const [tip, setTip] = useState<TipState | null>(null);

  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const iw = Math.max(40, width - pad.l - pad.r);
  const ih = height - pad.t - pad.b;

  const totals = data.map((d) =>
    stacked
      ? series.reduce((a, s) => a + (Number(d[s.key]) || 0), 0)
      : Math.max(...series.map((s) => Number(d[s.key]) || 0)),
  );
  const yMax = niceMax(Math.max(1, ...totals));
  const py = (v: number) => pad.t + ih - (v / yMax) * ih;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);

  const slot = iw / data.length;
  const barGroupW = Math.min(slot * 0.62, 48);
  const colors = series.map((s, i) => s.color ?? seriesColor(i));
  const labelEvery = Math.max(1, Math.ceil(data.length / maxTicks));

  return (
    <div className="viz" ref={ref} style={{ height }}>
      <svg width={width} height={height} onMouseLeave={() => setTip(null)}>
        {yTicks.map((v) => (
          <g key={v}>
            <line className={v === 0 ? "baseline" : "grid-line"} x1={pad.l} x2={pad.l + iw} y1={py(v)} y2={py(v)} />
            <text className="axis-tick" x={pad.l - 8} y={py(v) + 3.5} textAnchor="end">{yFmt(v)}</text>
          </g>
        ))}
        {data.map((d, i) => {
          const cx = pad.l + slot * i + slot / 2;
          const showTip = (e: React.MouseEvent) => {
            const rect = (e.currentTarget as SVGElement).closest("svg")!.getBoundingClientRect();
            setTip({
              x: e.clientX - rect.left,
              y: 24,
              title: String(d[x]),
              rows: series.map((s, si) => ({ label: s.label, value: yFmt(Number(d[s.key]) || 0), color: colors[si] })),
            });
          };
          return (
            <g key={i} onMouseMove={showTip}>
              {/* transparent hit target wider than the mark */}
              <rect x={cx - slot / 2} y={pad.t} width={slot} height={ih} fill="transparent" />
              {i % labelEvery === 0 && (
                <text x={cx} y={height - 8} textAnchor="middle">{String(d[x])}</text>
              )}
              {stacked
                ? (() => {
                    let acc = 0;
                    return series.map((s, si) => {
                      const v = Number(d[s.key]) || 0;
                      if (v <= 0) return null;
                      const y0 = py(acc);
                      const y1 = py(acc + v);
                      acc += v;
                      const topSeg = acc >= totals[i] - 0.001;
                      return (
                        <rect
                          key={s.key}
                          x={cx - barGroupW / 2}
                          y={y1}
                          width={barGroupW}
                          height={Math.max(1.5, y0 - y1 - 2)} /* 2px surface gap between segments */
                          rx={topSeg ? 4 : 1.5}
                          fill={colors[si]}
                        />
                      );
                    });
                  })()
                : series.map((s, si) => {
                    const v = Number(d[s.key]) || 0;
                    const bw = (barGroupW - (series.length - 1) * 2) / series.length;
                    const bx = cx - barGroupW / 2 + si * (bw + 2);
                    return (
                      <rect
                        key={s.key}
                        x={bx}
                        y={py(v)}
                        width={bw}
                        height={Math.max(0, py(0) - py(v))}
                        rx={Math.min(4, bw / 2)}
                        fill={colors[si]}
                      />
                    );
                  })}
            </g>
          );
        })}
      </svg>
      {tip && <Tip tip={tip} width={width} />}
    </div>
  );
}

/* ---------- Donut ---------- */
export function Donut({ data, size = 168, centerValue, centerLabel, thickness = 20 }: {
  data: { label: string; value: number; color?: string }[];
  size?: number;
  centerValue?: string;
  centerLabel?: string;
  thickness?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const total = data.reduce((a, d) => a + d.value, 0) || 1;
  const r = size / 2 - 6;
  const c = size / 2;
  let acc = 0;

  return (
    <div className="row gap-20 wrap" style={{ alignItems: "center" }}>
      <svg width={size} height={size} role="img" aria-label={centerLabel}>
        {data.map((d, i) => {
          const start = (acc / total) * Math.PI * 2 - Math.PI / 2;
          acc += d.value;
          const end = (acc / total) * Math.PI * 2 - Math.PI / 2;
          const large = end - start > Math.PI ? 1 : 0;
          const p1 = [c + r * Math.cos(start), c + r * Math.sin(start)];
          const p2 = [c + r * Math.cos(end), c + r * Math.sin(end)];
          return (
            <path
              key={i}
              d={`M${p1[0]} ${p1[1]} A${r} ${r} 0 ${large} 1 ${p2[0]} ${p2[1]}`}
              fill="none"
              stroke={d.color ?? seriesColor(i)}
              strokeWidth={hover === i ? thickness + 4 : thickness}
              /* 2px surface gap between segments via round dash trim */
              strokeLinecap="butt"
              strokeDasharray={`${Math.max(0, (end - start) * r - 2)} 1000`}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              style={{ transition: "stroke-width 0.12s" }}
            />
          );
        })}
        <text className="viz-donut-center-value" x={c} y={c + 2} textAnchor="middle">
          {hover !== null ? `${Math.round((data[hover].value / total) * 100)}%` : centerValue}
        </text>
        <text className="viz-donut-center-label" x={c} y={c + 20} textAnchor="middle">
          {hover !== null ? data[hover].label : centerLabel}
        </text>
      </svg>
      <div className="col gap-6">
        {data.map((d, i) => (
          <span className="viz-legend-item" key={d.label} style={{ fontSize: 12.5, color: "var(--ink-2)", fontWeight: 550 }}>
            <span className="viz-legend-swatch" style={{ background: d.color ?? seriesColor(i) }} />
            {d.label}
            <span className="t-num t-strong" style={{ color: "var(--ink)", marginLeft: 4 }}>
              {Math.round((d.value / total) * 100)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

/* ---------- Horizontal bar list (top-N rankings) ---------- */
export function HBarList({ data, color = "var(--series-1)", valueFmt = fmtNum, trend }: {
  data: { label: string; value: number }[];
  color?: string;
  valueFmt?: (v: number) => string;
  trend?: number[]; // optional per-row trend %
}) {
  const max = Math.max(1, ...data.map((d) => d.value));
  return (
    <div className="col gap-12">
      {data.map((d, i) => (
        <div key={d.label} className="col gap-4">
          <div className="row-between" style={{ fontSize: 12.5 }}>
            <span className="t-sub truncate" style={{ fontWeight: 550 }}>{d.label}</span>
            <span className="row gap-6">
              <span className="t-num t-strong">{valueFmt(d.value)}</span>
              {trend && trend[i] !== undefined && (
                <span className={`t-micro t-num ${trend[i] >= 0 ? "" : ""}`} style={{ color: "var(--ink-3)" }}>
                  {trend[i] >= 0 ? "+" : ""}{trend[i]}%
                </span>
              )}
            </span>
          </div>
          <div className="progress" style={{ height: 8 }}>
            <div className="progress-fill" style={{ width: `${(d.value / max) * 100}%`, background: color }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---------- Chart card wrapper ---------- */
export function ChartCard({ title, sub, legend, children, right }: {
  title: string; sub?: string; legend?: ReactNode; children: ReactNode; right?: ReactNode;
}) {
  return (
    <div className="card">
      <div className="card-header" style={{ borderBottom: "none", paddingBottom: 4 }}>
        <div className="col gap-2">
          <span className="card-title">{title}</span>
          {sub && <span className="t-micro">{sub}</span>}
        </div>
        {right}
      </div>
      <div style={{ padding: "4px 20px 18px" }}>
        {legend && <div style={{ marginBottom: 10 }}>{legend}</div>}
        {children}
      </div>
    </div>
  );
}

function niceMax(v: number): number {
  const mag = 10 ** Math.floor(Math.log10(v));
  const norm = v / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return nice * mag;
}
