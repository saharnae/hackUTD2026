// Scrolling ticker tape (SIMULATED quotes) pinned above the nav.
import { useEffect, useState } from "react";

const BASE: [string, number][] = [
  ["SPY", 584.2], ["QQQ", 506.4], ["IWM", 226.1], ["DIA", 431.8], ["VIX", 15.9], ["TLT", 92.3],
  ["GLD", 247.6], ["XLF", 47.2], ["AAPL", 231.5], ["NVDA", 142.8], ["MSFT", 428.1], ["AMZN", 201.3],
  ["META", 586.9], ["TSLA", 252.4], ["BTC", 68420], ["ETH", 3410],
];

export default function Ticker() {
  const [q, setQ] = useState(() => BASE.map(([s, p]) => ({ s, p, chg: (Math.random() - 0.45) * 1.6 })));
  useEffect(() => {
    const id = setInterval(() => setQ((xs) => xs.map((x) => {
      const d = (Math.random() - 0.5) * 0.12;
      return { ...x, p: x.p * (1 + d / 100), chg: Math.max(-4, Math.min(4, x.chg + d)) };
    })), 1600);
    return () => clearInterval(id);
  }, []);
  const row = q.map((x) => (
    <span key={x.s} className="tk">
      <b>{x.s}</b>
      <span className="tk-px">{x.p >= 1000 ? x.p.toLocaleString("en-US", { maximumFractionDigits: 0 }) : x.p.toFixed(2)}</span>
      <span className={x.chg >= 0 ? "up" : "down"}>{x.chg >= 0 ? "▲" : "▼"} {Math.abs(x.chg).toFixed(2)}%</span>
    </span>
  ));
  return (
    <div className="ticker" aria-label="Simulated quotes">
      <span className="tk-label">SIMULATED</span>
      <div className="tk-viewport">
        <div className="tk-track">{row}{row}</div>
      </div>
    </div>
  );
}
