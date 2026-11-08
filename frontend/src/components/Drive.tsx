// Pinned, scroll-driven road: the section sticks while you scroll, the ρ car drives the road,
// and each stop of the supervisor loop lights up as the car reaches it.
// Performance: scroll updates write straight to the SVG (no React re-render per frame);
// React only re-renders when the active stop changes.
import { useEffect, useRef, useState } from "react";
import type { SignKind } from "./Signs";
import Icon from "./Icons";

const SIGN_Y = -48;   // all signs sit in one row above the road
const ROAD = "M40,150 C200,150 230,70 380,70 C540,70 560,190 720,190 C880,190 900,80 1060,80 C1220,80 1250,150 1400,150";

export type StopDef = { kind: SignKind; text: string; name: string; icon: string; tool: string; body: string };

/** Where along the road (0..1 of its length) stop i sits. The page snaps to exactly these. */
export const stopAt = (i: number, n: number) => 0.08 + (i * 0.84) / (n - 1);

export default function Drive({ stops }: { stops: StopDef[] }) {
  const sec = useRef<HTMLElement>(null);
  const path = useRef<SVGPathElement>(null);
  const carRef = useRef<SVGGElement>(null);
  const glowRef = useRef<SVGPathElement>(null);
  const laneRef = useRef<SVGPathElement>(null);
  const meterRef = useRef<HTMLSpanElement>(null);
  const [pts, setPts] = useState<{ x: number; y: number }[]>([]);
  const [active, setActive] = useState(-1);
  const n = stops.length;

  useEffect(() => {
    const el = path.current!;
    const L = el.getTotalLength();
    setPts(stops.map((_, i) => { const q = el.getPointAtLength(L * stopAt(i, n)); return { x: q.x, y: q.y }; }));
  }, [stops, n]);

  useEffect(() => {
    const el = path.current!, L = el.getTotalLength();
    const S = Array.from({ length: n }, (_, i) => stopAt(i, n));
    const HOLD = 0.6;                                   // share of each scroll leg the car stays parked
    const easeIO = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
    // scroll progress (0..1) -> where the car should be (fraction of road), parked on stops most of the time
    const target = (v: number) => {
      const t = v * (n - 1), k = Math.min(n - 2, Math.floor(t)), f = t - k;
      if (v >= 1) return S[n - 1];
      const m = f <= HOLD ? 0 : easeIO((f - HOLD) / (1 - HOLD));
      return S[k] + (S[k + 1] - S[k]) * m;
    };
    let raf = 0, last = -2, shown = -1, goal = S[0], idle = 0;
    const nearest = (x: number) => S.reduce((a, b) => (Math.abs(b - x) < Math.abs(a - x) ? b : a), S[0]);

    const draw = (d: number) => {
      const q = el.getPointAtLength(L * d), q2 = el.getPointAtLength(Math.min(L, L * d + 6));
      const ang = Math.atan2(q2.y - q.y, q2.x - q.x) * 57.3 * 0.35;
      carRef.current?.setAttribute("transform", `translate(${q.x.toFixed(2)},${q.y.toFixed(2)}) rotate(${ang.toFixed(2)})`);
      if (glowRef.current) glowRef.current.style.strokeDashoffset = `${1 - d}`;
      if (laneRef.current) laneRef.current.style.strokeDashoffset = `${-d * 900}`;
      let a = -1;
      for (let i = 0; i < n; i++) if (d >= S[i] - 0.004) a = i;
      if (a !== last) { last = a; setActive(a); }
    };
    // glide the car toward its goal so it eases into each stop instead of following every wheel tick
    const tick = () => {
      raf = 0;
      if (shown < 0) shown = goal;
      const diff = goal - shown;
      shown = Math.abs(diff) < 0.0005 ? goal : shown + diff * 0.16;
      draw(shown);
      if (shown !== goal) raf = requestAnimationFrame(tick);
    };
    const onScroll = () => {
      const r = sec.current!.getBoundingClientRect();
      const v = Math.max(0, Math.min(1, -r.top / Math.max(1, r.height - window.innerHeight)));
      if (meterRef.current) meterRef.current.style.transform = `scaleX(${v})`;
      // stops 1-5 use the first 80% of the pinned scroll (parking on each); after exit 5 the car
      // simply keeps driving to the end of the road with your scroll, and the green line follows
      const END = 0.8;
      goal = v <= END ? target(v / END) : S[n - 1] + (1 - S[n - 1]) * ((v - END) / (1 - END));
      if (!raf) raf = requestAnimationFrame(tick);
      // when scrolling pauses between stops, settle on the nearest one; never pull back past exit 5
      clearTimeout(idle);
      if (goal < S[n - 1] - 1e-4) {
        idle = window.setTimeout(() => { goal = nearest(goal); if (!raf) raf = requestAnimationFrame(tick); }, 140);
      }
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => { window.removeEventListener("scroll", onScroll); window.removeEventListener("resize", onScroll); cancelAnimationFrame(raf); clearTimeout(idle); };
  }, [n]);

  const cur = stops[Math.max(0, active)];

  return (
    <section id="supervisor" ref={sec} className="drive" data-snap
      data-stops={stops.map((_, i) => stopAt(i, n).toFixed(4)).join(",")}
      style={{ height: `${n * 75 + 170}vh` }}>
      <div className="drive-stick">
        <div className="wrap">
          <div className="mono-tag">[ 04 / SUPERVISOR ] <span className="dim">· scroll to drive</span></div>
          <h2 className="display-h">The supervisor drives the loop.</h2>
          <p className="lede">An orchestrator and four Nemotron agents watch the system, call real tools at every exit,
            and stop for a human before anything risky changes.</p>
        </div>

        <div className="drive-road">
          <svg viewBox="0 -90 1440 340" preserveAspectRatio="xMidYMid meet">
            <path ref={path} d={ROAD} fill="none" stroke="rgba(255,255,255,.09)" strokeWidth="58" strokeLinecap="round" />
            <path d={ROAD} fill="none" stroke="#14181c" strokeWidth="52" strokeLinecap="round" />
            <path ref={laneRef} d={ROAD} fill="none" stroke="#e8b800" strokeWidth="2.5" strokeDasharray="16 14" />
            {/* the stretch already driven glows green */}
            <path ref={glowRef} d={ROAD} fill="none" stroke="rgba(0,200,5,.6)" strokeWidth="3" strokeLinecap="round"
              pathLength={1} strokeDasharray="1" style={{ strokeDashoffset: 1 }} />
            {pts.map((q, i) => {
              const st = stops[i], on = i <= active, here = i === active;
              return (
                <g key={i} className={`drive-stop ${on ? "on" : ""} ${here ? "here" : ""}`}>
                  <line x1={q.x} y1={SIGN_Y + 26} x2={q.x} y2={q.y - 11} className="drive-post" />
                  <circle cx={q.x} cy={q.y} r="9" className="drive-dot" />
                  <text x={q.x} y={q.y + 40} textAnchor="middle" className="drive-name">{st.name}</text>
                  <g transform={`translate(${q.x},${SIGN_Y})`}>
                    <g className="drive-sign">
                      {st.kind === "stop" ? (
                        <>
                          <polygon points="-11,-27 11,-27 27,-11 27,11 11,27 -11,27 -27,11 -27,-11" fill="#c8102e" stroke="#fff" strokeWidth="3" />
                          <text y="5" textAnchor="middle" className="sg-stop">STOP</text>
                        </>
                      ) : (
                        <>
                          <rect x="-8" y="-38" width="38" height="15" rx="4" fill="#0b6b3a" stroke="#fff" strokeWidth="2" />
                          <text x="11" y="-27.5" textAnchor="middle" className="sg-tab">EXIT</text>
                          <rect x="-36" y="-24" width="72" height="50" rx="8" fill="#0b6b3a" />
                          <rect x="-32" y="-20" width="64" height="42" rx="5" fill="none" stroke="#fff" strokeWidth="2.5" />
                          <text y="10" textAnchor="middle" className="sg-num">{st.text}</text>
                        </>
                      )}
                    </g>
                  </g>
                </g>
              );
            })}
            {/* the car: centred on its own origin so it sits flush on each stop */}
            <g ref={carRef} className="drive-car" transform="translate(40,150)">
              <g transform="translate(-17,-18.4) scale(.34)">
                <path d="M50 4 C66 12 82 12 96 6 C100 40 92 78 50 104 C8 78 0 40 4 6 C18 12 34 12 50 4Z" fill="#1f4fbf" stroke="#fff" strokeWidth="6" />
                <path d="M8 8 C22 14 36 14 50 7 C64 14 78 14 92 8 L91 30 L9 30Z" fill="#c8102e" />
                <text x="50" y="84" textAnchor="middle" fontFamily="Inter, sans-serif" fontWeight="600" fontSize="58" fill="#fff">ρ</text>
              </g>
            </g>
          </svg>
        </div>

        <div className="wrap">
          <div className="drive-card" key={active}>
            {active < 0 ? (
              <p className="dim">Scroll to start. The ρ car is at the on-ramp.</p>
            ) : (<>
              <div className="dc-head">
                <span className="dc-step">STOP {active + 1} / {n}</span>
                <span className="dc-tool"><Icon name={cur.icon} size={14} /> {cur.tool}</span>
              </div>
              <h3>{cur.name}</h3>
              <p>{cur.body}</p>
            </>)}
          </div>
          <div className="drive-meter"><span ref={meterRef} /></div>
        </div>
      </div>
    </section>
  );
}
