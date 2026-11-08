// Sticky, blurred top bar shared by the site and the desk.
import { useEffect, useState, type ReactNode } from "react";
import { RhoShield } from "./Signs";
import Ticker from "./Ticker";

export const TABS = [
  { id: "network", label: "Network" },
  { id: "overview", label: "Overview" },
  { id: "strategy", label: "Strategy" },
  { id: "supervisor", label: "Supervisor" },
  { id: "safety", label: "Safety" },
];

export default function Nav({ mode, onLaunch, onHome, children }: {
  mode: "site" | "desk"; onLaunch?: () => void; onHome?: () => void; children?: ReactNode;
}) {
  const [active, setActive] = useState("network");
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    if (mode !== "site") return () => window.removeEventListener("scroll", onScroll);
    const io = new IntersectionObserver(
      (es) => es.forEach((e) => e.isIntersecting && setActive(e.target.id)),
      { rootMargin: "-45% 0px -50% 0px" },
    );
    TABS.forEach((t) => { const el = document.getElementById(t.id); if (el) io.observe(el); });
    return () => { window.removeEventListener("scroll", onScroll); io.disconnect(); };
  }, [mode]);

  const jump = (id: string) => { const el = document.getElementById(id); if (!el) return; const f = (window as any).__snapTo; f ? f(el) : el.scrollIntoView({ behavior: "smooth", block: "start" }); };

  return (
    <nav className={`nav ${scrolled ? "scrolled" : ""}`}>
      {mode === "site" && <Ticker />}
      <div className="nav-inner">
        <button className="nav-brand" onClick={mode === "desk" ? onHome : () => window.scrollTo({ top: 0, behavior: "smooth" })}>
          <RhoShield size={26} />
          <span>Rho to Riches</span>
        </button>

        {mode === "site" ? (
          <div className="tabs" role="tablist">
            {TABS.map((t) => (
              <button key={t.id} role="tab" aria-selected={active === t.id}
                className={`tab ${active === t.id ? "on" : ""}`} onClick={() => jump(t.id)}>
                {t.label}
              </button>
            ))}
          </div>
        ) : (
          <div className="tabs"><button className="tab" onClick={onHome}>← Back to site</button><span className="tab on">Desk</span></div>
        )}

        <div className="nav-right">
          {children}
          {mode === "site" && <button className="btn primary sm" onClick={onLaunch}>Run ρ in your browser</button>}
        </div>
      </div>
    </nav>
  );
}
