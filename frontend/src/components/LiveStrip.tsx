// Robinhood-style price + chart, Webull/IBKR-style dense stat strip. Polls /api/state.
import { useEffect, useRef, useState } from "react";

type Pt = { t: number; px: number };

function Sparkline({ pts, up }: { pts: Pt[]; up: boolean }) {
  if (pts.length < 2) return <div className="spark empty" />;
  const W = 640, H = 120, pad = 6;
  const xs = pts.map((p) => p.t), ys = pts.map((p) => p.px);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = (t: number) => ((t - x0) / Math.max(1, x1 - x0)) * W;
  const sy = (v: number) => H - pad - ((v - y0) / Math.max(0.01, y1 - y0)) * (H - pad * 2);
  // smooth path (Catmull-Rom -> Bezier) for the flowy Robinhood line
  const P = pts.map((p) => [sx(p.t), sy(p.px)]);
  let d = `M${P[0][0]},${P[0][1]}`;
  for (let i = 0; i < P.length - 1; i++) {
    const [a, b, c, e] = [P[i - 1] ?? P[i], P[i], P[i + 1], P[i + 2] ?? P[i + 1]];
    d += ` C${b[0] + (c[0] - a[0]) / 6},${b[1] + (c[1] - a[1]) / 6} ${c[0] - (e[0] - b[0]) / 6},${c[1] - (e[1] - b[1]) / 6} ${c[0]},${c[1]}`;
  }
  const color = up ? "var(--up)" : "var(--down)";
  const last = P[P.length - 1];
  return (
    <svg className="spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.28" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d} L${last[0]},${H} L${P[0][0]},${H} Z`} fill="url(#fill)" />
      <path d={d} fill="none" stroke={color} strokeWidth="2.5" vectorEffect="non-scaling-stroke" />
      <circle cx={last[0]} cy={last[1]} r="4" fill={color} className="pulse" />
    </svg>
  );
}

function Stat({ label, value, tone, bar }: { label: string; value: string; tone?: string; bar?: number }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {bar !== undefined && <div className="stat-bar"><span style={{ width: `${Math.min(100, bar * 100)}%` }} /></div>}
    </div>
  );
}

export default function LiveStrip() {
  const [st, setSt] = useState<any>();
  const [pts, setPts] = useState<Pt[]>([]);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const tick = async () => {
      try {
        const s = await fetch("/api/state").then((r) => r.json());
        if (!alive.current) return;
        setSt(s);
        if (typeof s.last_price === "number")
          setPts((p) => [...p.slice(-119), { t: Date.now(), px: s.last_price }]);
      } catch { /* backend restarting */ }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { alive.current = false; clearInterval(id); };
  }, []);

  if (!st) return <div className="live card"><span className="muted">Connecting to the trading system…</span></div>;

  const px = st.last_price, open = st.open_price ?? pts[0]?.px;
  const chg = px != null && open != null ? px - open : 0;
  const up = chg >= 0;
  const g = st.gate ?? {};
  const gate = g.killed ? "KILLED" : g.latched ? "LATCHED" : g.armed ? "ARMED" : "DISARMED";
  const delta = st.position?.net_delta, lim = st.config?.max_delta;
  const pnl = st.pnl_today;

  return (
    <div className="live card">
      <div className="quote">
        <div className="sym">{st.position?.symbol ?? "—"} <span className="muted small">· {st.source ?? ""}</span></div>
        {px != null && <div className="px">${px.toFixed(2)}</div>}
        {px != null && (
          <div className={`chg ${up ? "up" : "down"}`}>
            {up ? "▲" : "▼"} {up ? "+" : ""}{chg.toFixed(2)} ({((chg / open) * 100).toFixed(2)}%) <span className="muted">today</span>
          </div>
        )}
        <Sparkline pts={pts} up={up} />
      </div>
      <div className="stats">
        <Stat label="Risk gate" value={gate} tone={gate === "ARMED" ? "ok" : "bad"} />
        <Stat label="Net Δ / limit" value={delta != null ? `${delta} / ${lim}` : "—"}
          tone={delta > lim ? "bad" : "ok"} bar={delta != null && lim ? delta / lim : undefined} />
        <Stat label="P&L today" value={pnl != null ? `${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl).toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—"}
          tone={pnl >= 0 ? "ok" : "bad"} />
        <Stat label="Feed age" value={st.feed_age_ms != null ? `${st.feed_age_ms} ms` : "—"}
          tone={st.feed_age_ms > (st.config?.stale_feed_ms ?? 500) ? "bad" : "ok"} />
        <Stat label="Bad frames / min" value={`${st.bad_frames_1m ?? "—"}`} tone={st.bad_frames_1m > 10 ? "bad" : "ok"} />
        <Stat label="Token bucket" value={`${st.token_bucket ?? "—"}`} tone={st.token_bucket < 5 ? "bad" : "ok"}
          bar={st.token_bucket != null ? st.token_bucket / 20 : undefined} />
        {g.trip_reason && <div className="trip"><b>TRIP</b> {g.trip_reason}</div>}
      </div>
    </div>
  );
}
