// Robinhood-style chart: one smooth line, timeframe tabs, hover to scrub the price.
import { useMemo, useRef, useState } from "react";

export type Pt = { t: number; px: number };

function rng(seed: number) {
  return () => { seed |= 0; seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

export const FRAMES = [
  { id: "1D", n: 78, step: 5 * 60e3, vol: 0.0009, seed: 11 },
  { id: "1W", n: 130, step: 3 * 3600e3, vol: 0.0022, seed: 12 },
  { id: "1M", n: 120, step: 6 * 3600e3, vol: 0.0038, seed: 13 },
  { id: "3M", n: 90, step: 86400e3, vol: 0.0085, seed: 14 },
  { id: "1Y", n: 252, step: 86400e3, vol: 0.0095, seed: 15 },
] as const;

/** Deterministic SIMULATED series that ends at `last`. */
export function series(frame: (typeof FRAMES)[number], last: number): Pt[] {
  const r = rng(frame.seed);
  const out: Pt[] = [];
  let px = last;
  const now = Date.now();
  for (let i = 0; i < frame.n; i++) {
    out.push({ t: now - i * frame.step, px });
    const z = (r() + r() + r() - 1.5) * 2;           // ~N(0,1)
    px = px / (1 + frame.vol * z + frame.vol * 0.08);
  }
  out.reverse();
  // light EMA so the line reads calm, like a brokerage chart, then pin the last point to `last`
  let ema = out[0].px;
  const sm = out.map((p) => { ema = ema + 0.38 * (p.px - ema); return { t: p.t, px: ema }; });
  const shift = last - sm[sm.length - 1].px;
  return sm.map((p) => ({ t: p.t, px: p.px + shift }));
}

function smooth(P: number[][]) {
  let d = `M${P[0][0]},${P[0][1]}`;
  for (let i = 0; i < P.length - 1; i++) {
    const a = P[i - 1] ?? P[i], b = P[i], c = P[i + 1], e = P[i + 2] ?? c;
    d += ` C${b[0] + (c[0] - a[0]) / 6},${b[1] + (c[1] - a[1]) / 6} ${c[0] - (e[0] - b[0]) / 6},${c[1] - (e[1] - b[1]) / 6} ${c[0]},${c[1]}`;
  }
  return d;
}

const fmtT = (t: number, f: string) => {
  const d = new Date(t);
  return f === "1D" ? d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(f === "1Y" ? { year: "numeric" } : {}) });
};

export default function PriceChart({ symbol, last }: { symbol: string; last: number }) {
  const [frame, setFrame] = useState<(typeof FRAMES)[number]["id"]>("1D");
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<SVGSVGElement>(null);
  const f = FRAMES.find((x) => x.id === frame)!;
  const pts = useMemo(() => series(f, last), [f, last]);

  const W = 1000, H = 280, pad = 14;
  const ys = pts.map((p) => p.px);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => pad + (1 - (v - lo) / Math.max(1e-6, hi - lo)) * (H - pad * 2);
  const P = pts.map((p, i) => [x(i), y(p.px)]);
  const d = useMemo(() => smooth(P), [pts]);   // eslint-disable-line

  const first = pts[0].px;
  const cur = hover != null ? pts[hover] : pts[pts.length - 1];
  const chg = cur.px - first;
  const up = chg >= 0;
  const base = y(first);

  const onMove = (e: React.PointerEvent) => {
    const r = box.current!.getBoundingClientRect();
    const i = Math.round(((e.clientX - r.left) / r.width) * (pts.length - 1));
    setHover(Math.max(0, Math.min(pts.length - 1, i)));
  };

  return (
    <div className={`chart ${up ? "is-up" : "is-down"}`}>
      <div className="quote-head">
        <div className="q-sym">{symbol} <span className="pill-sim">Simulated</span></div>
        <div className="q-px">${cur.px.toFixed(2)}</div>
        <div className="q-chg">
          {up ? "+" : "−"}${Math.abs(chg).toFixed(2)} ({up ? "+" : "−"}{Math.abs((chg / first) * 100).toFixed(2)}%)
          <span className="q-when">{hover != null ? fmtT(cur.t, frame) : frame === "1D" ? "Today" : `Past ${frame}`}</span>
        </div>
      </div>

      <svg ref={box} className="chart-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <line x1="0" x2={W} y1={base} y2={base} className="baseline" vectorEffect="non-scaling-stroke" />
        <path key={frame} d={d} className="line draw" vectorEffect="non-scaling-stroke" pathLength={1} />
        {hover != null && <>
          <line x1={P[hover][0]} x2={P[hover][0]} y1="0" y2={H} className="scrub" vectorEffect="non-scaling-stroke" />
        </>}
      </svg>
      {hover != null && (
        <div className="scrub-dot" style={{ left: `${(P[hover][0] / W) * 100}%`, top: `${(P[hover][1] / H) * 100}%` }} />
      )}

      <div className="frames" role="tablist">
        {FRAMES.map((x) => (
          <button key={x.id} role="tab" aria-selected={frame === x.id}
            className={`frame ${frame === x.id ? "on" : ""}`} onClick={() => setFrame(x.id)}>{x.id}</button>
        ))}
      </div>
    </div>
  );
}

export function stats(last: number) {
  const day = series(FRAMES[0], last), yr = series(FRAMES[4], last);
  const d = day.map((p) => p.px), y = yr.map((p) => p.px);
  return {
    open: day[0].px, high: Math.max(...d), low: Math.min(...d), prev: day[0].px * 0.9987,
    hi52: Math.max(...y), lo52: Math.min(...y),
  };
}
