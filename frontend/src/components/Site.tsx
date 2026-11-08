// Landing page: a trading-platform site with a night-drive road theme and scroll-driven motion.
import { useEffect, useRef, useState } from "react";
import Nav from "./Nav";
import PriceChart, { stats } from "./PriceChart";
import WindingRoad from "./WindingRoad";
import NightField from "./NightField";
import Globe from "./Globe";
import Drive, { type StopDef } from "./Drive";
import Icon from "./Icons";
import { SignBullet, type SignKind } from "./Signs";
import { CountUp, Split } from "./Motion";

const HERO_ROAD = "M-40,120 C180,120 260,40 470,52 C700,66 720,168 960,160 C1180,152 1220,70 1480,84";
const END_ROAD = "M-40,70 C240,70 320,130 600,122 C880,114 920,40 1180,46 C1320,50 1400,80 1480,78";

const STOPS: StopDef[] = [
  { kind: "exit", text: "1", name: "Monitor", icon: "eye", tool: "observe · detect",
    body: "Reads the board's status and log frames and flags trouble: a tripped gate, a stale feed, bad frames, a thin token bucket." },
  { kind: "exit", text: "2", name: "Stress test", icon: "flask", tool: "simulate · seeds 0–79",
    body: "Replays the current state forward on simulated seeds to forecast what happens if nobody acts. Seeds 80–99 stay held out." },
  { kind: "exit", text: "3", name: "Proposal", icon: "route", tool: "list_actions · check_action · simulate",
    body: "Lists candidate fixes, runs every one through the hard-rule check, simulates the ones that pass, and ranks them." },
  { kind: "stop", text: "STOP", name: "You decide", icon: "user", tool: "accept · deny",
    body: "A person accepts or denies the recommended route. Nothing that adds risk changes without that click. A kill can go through on its own, because it only makes things safer." },
  { kind: "exit", text: "5", name: "Post-mortem", icon: "file", tool: "audit_log · report_template",
    body: "Writes the incident report from the hash-chained audit log and cites each entry, so anyone can check what happened and who approved it." },
];

function useReveal() {
  useEffect(() => {
    const io = new IntersectionObserver((es) => es.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
    }), { rootMargin: "0px 0px -10% 0px" });
    document.querySelectorAll(".reveal, .scene, .split").forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

function useScrollVars() {
  // compositor-only updates: the progress bar and the background glow move with transforms
  useEffect(() => {
    const bar = document.getElementById("progress");
    const light = document.querySelector<HTMLElement>(".site .light");
    let raf = 0;
    const frame = () => {
      raf = 0;
      const full = document.documentElement.scrollHeight - window.innerHeight;
      const f = full > 0 ? Math.min(1, window.scrollY / full) : 0;
      if (bar) bar.style.transform = `scaleX(${f.toFixed(4)})`;
      if (light) light.style.transform = `translate3d(0, ${(f * -60).toFixed(2)}vh, 0)`;
    };
    const on = () => { if (!raf) raf = requestAnimationFrame(frame); };
    frame();
    window.addEventListener("scroll", on, { passive: true });
    return () => { window.removeEventListener("scroll", on); cancelAnimationFrame(raf); };
  }, []);
}

function useLive() {
  const [st, setSt] = useState<any>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => fetch("/api/state").then((r) => r.json()).then((s) => alive && setSt(s)).catch(() => alive && setSt(null));
    tick();
    const id = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  return st;
}

/** Floating cards that drift at different speeds on scroll and tilt toward the mouse. */
function FloatCards({ live }: { live: any }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current!;
    const move = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${((e.clientX - r.left) / r.width - 0.5).toFixed(3)}`);
      el.style.setProperty("--my", `${((e.clientY - r.top) / r.height - 0.5).toFixed(3)}`);
    };
    window.addEventListener("pointermove", move);
    return () => window.removeEventListener("pointermove", move);
  }, []);
  const g = live?.gate;
  const gate = !live ? "OFFLINE" : g?.killed ? "KILLED" : g?.latched ? "LATCHED" : g?.armed ? "ARMED" : "DISARMED";
  const d = live?.position?.net_delta ?? 12, lim = live?.config?.max_delta ?? 20;
  const pnl = live?.pnl_today;
  return (
    <div className="floats" ref={box} aria-hidden>
      <div className="fc fc-a" style={{ ["--d" as any]: 0.18 }}>
        <div className="fc-k"><Icon name="shield" size={13} /> RISK GATE · LIVE</div>
        <div className={`fc-v ${gate === "ARMED" ? "up" : gate === "OFFLINE" ? "" : "down"}`}>{gate}</div>
        <div className="fc-s">Rules R01–R15, checked in hardware</div>
      </div>
      <div className="fc fc-b" style={{ ["--d" as any]: 0.32 }}>
        <div className="fc-k"><Icon name="gauge" size={13} /> NET Δ / LIMIT</div>
        <div className="fc-v big">{d}<span className="dim"> / {lim}</span></div>
        <div className="fc-bar"><span style={{ width: `${Math.min(100, (d / lim) * 100)}%` }} className={d > lim ? "bad" : ""} /></div>
      </div>
      <div className="fc fc-c" style={{ ["--d" as any]: 0.1 }}>
        <div className="fc-k"><Icon name="pulse" size={13} /> P&L TODAY · SIM</div>
        <div className={`fc-v ${pnl == null ? "" : pnl >= 0 ? "up" : "down"}`}>
          {pnl == null ? "—" : `${pnl >= 0 ? "+" : "−"}$${Math.abs(pnl).toFixed(0)}`}
        </div>
      </div>
    </div>
  );
}

const FEED_VERBS = ["route accepted", "gate check passed", "stale feed flagged", "kill requested", "config proposed", "post-mortem filed"];

export default function Site({ onLaunch }: { onLaunch: () => void }) {
  useReveal();
  useScrollVars();
  const live = useLive();
  const last = 584.2;
  const k = stats(last);

  // simulated network activity beside the globe
  const [online, setOnline] = useState(1284);
  const [feed, setFeed] = useState<{ id: number; txt: string; ok: boolean }[]>([]);
  const fid = useRef(0);
  useEffect(() => {
    const id = setInterval(() => setOnline((n) => Math.max(900, n + Math.round((Math.random() - 0.45) * 9))), 1200);
    return () => clearInterval(id);
  }, []);
  const onArc = (a: string, b: string) => {
    if (Math.random() > 0.55) return;
    const verb = FEED_VERBS[Math.floor(Math.random() * FEED_VERBS.length)];
    setFeed((f) => [{ id: fid.current++, txt: `${a} → ${b} · ${verb}`, ok: !verb.includes("kill") && !verb.includes("stale") }, ...f].slice(0, 3));
  };

  const jump = (id: string) => { const el = document.getElementById(id); if (!el) return; const f = (window as any).__snapTo; f ? f(el) : el.scrollIntoView({ behavior: "smooth" }); };
  const head = (n: string, label: string, kind: SignKind, text?: string) => (
    <div className="mono-tag reveal"><SignBullet kind={kind} text={text} /><span>[ {n} / {label} ]</span></div>
  );

  return (
    <div className="site">
      <NightField />
      <div className="light" aria-hidden />
      <div className="grain" aria-hidden />
      <div id="progress" aria-hidden />
      <Nav mode="site" onLaunch={onLaunch} />

      {/* ---------- hero ---------- */}
      <section className="hero">
        <div className="wrap hero-grid">
          <div className="hero-copy">
            <div className="mono-tag reveal"><span className="tag-dot" /> [ HACKUTD 2026 ] · FPGA × NEMOTRON</div>
            <Split as="h1" text="Rho to Riches." className="display-xl" />
            <p className="hero-sub reveal">
              Trade without emotion. A rules-based options strategy, repriced on an FPGA behind a hardware risk gate,
              and watched around the clock by a Nemotron supervisor.
            </p>
            <div className="hero-cta reveal">
              <button className="btn primary lg" onClick={onLaunch}><Icon name="bolt" size={16} /> Run ρ in your browser</button>
              <button className="btn ghost lg" onClick={() => jump("supervisor")}>Watch it drive</button>
            </div>
            <div className="mono-note reveal">All market data simulated · no account needed</div>
          </div>
          <FloatCards live={live} />
        </div>
        <div className="hero-road"><WindingRoad id="hero-road" d={HERO_ROAD} height={200} dur={16} /></div>
        <div className="scroll-cue" aria-hidden><span>SCROLL</span><i /></div>
      </section>

      {/* ---------- network: the globe ---------- */}
      <section id="network" className="sec scene" data-snap>
        <div className="wrap net-grid inner">
          <div>
            {head("01", "NETWORK", "exit", "1")}
            <Split text="Every lane, watched live." />
            <p className="lede reveal">Each arc is an order crossing the risk gate between trading hubs. Green lights mean the
              gate checked it. Home base is Dallas.</p>
            <div className="net-stats reveal">
              <div><div className="ns-k"><Icon name="user" size={13} /> TRADERS ON THE ROAD</div><div className="ns-v"><CountUp to={online} live /></div></div>
              <div><div className="ns-k"><Icon name="shield" size={13} /> GATE CHECKS TODAY</div><div className="ns-v"><CountUp to={48210} /></div></div>
              <div><div className="ns-k"><Icon name="stop" size={13} /> KILLS REQUESTED</div><div className="ns-v"><CountUp to={3} /></div></div>
            </div>
            <ul className="feed-list" aria-live="polite">
              {feed.map((f) => (
                <li key={f.id} className={f.ok ? "ok" : "warn"}><i />{f.txt}</li>
              ))}
            </ul>
            <div className="mono-note">Simulated activity for the demo</div>
          </div>
          <div className="globe-wrap reveal"><Globe onArc={onArc} /></div>
        </div>
      </section>

      {/* ---------- overview: the stock page ---------- */}
      <section id="overview" className="sec scene" data-snap>
        <div className="wrap inner">
          {head("02", "OVERVIEW", "exit", "2")}
          <div className="two-col">
            <div className="col-main">
              <div className="reveal"><PriceChart symbol="SPY" last={last} /></div>
              <div className="block reveal">
                <h3 className="h3"><Icon name="gauge" size={18} /> Key statistics</h3>
                <dl className="kv">
                  <div><dt>Open</dt><dd>${k.open.toFixed(2)}</dd></div>
                  <div><dt>Today's high</dt><dd>${k.high.toFixed(2)}</dd></div>
                  <div><dt>Today's low</dt><dd>${k.low.toFixed(2)}</dd></div>
                  <div><dt>Prev close</dt><dd>${k.prev.toFixed(2)}</dd></div>
                  <div><dt>52 wk high</dt><dd>${k.hi52.toFixed(2)}</dd></div>
                  <div><dt>52 wk low</dt><dd>${k.lo52.toFixed(2)}</dd></div>
                  <div><dt>Net Δ limit</dt><dd>20 <span className="dim">R09</span></dd></div>
                  <div><dt>Data</dt><dd>Simulated</dd></div>
                </dl>
              </div>
            </div>
            <aside className="col-side">
              <div className="ticket reveal">
                <div className="ticket-head"><span>Desk status</span><span className={`dot ${live?.gate?.armed && !live?.gate?.latched ? "ok" : live ? "bad" : "off"}`} /></div>
                <dl className="ticket-rows">
                  <div><dt><Icon name="shield" size={14} /> Risk gate</dt><dd>{!live ? "Offline" : live.gate?.killed ? "Killed" : live.gate?.latched ? "Latched" : "Armed"}</dd></div>
                  <div><dt><Icon name="gauge" size={14} /> Net Δ / limit</dt><dd>{live ? `${live.position?.net_delta} / ${live.config?.max_delta}` : "—"}</dd></div>
                  <div><dt><Icon name="clock" size={14} /> Feed age</dt><dd>{live ? `${live.feed_age_ms} ms` : "—"}</dd></div>
                  <div><dt><Icon name="eye" size={14} /> Supervisor</dt><dd>Nemotron</dd></div>
                </dl>
                <button className="btn primary full" onClick={onLaunch}>Run ρ in your browser</button>
                <p className="fine">Rho trades SPY options by fixed rules, repriced on an FPGA behind a hardware risk gate. Open the desk, inject a hazard, and watch the supervisor work.</p>
              </div>
            </aside>
          </div>
        </div>
      </section>

      {/* ---------- strategy ---------- */}
      <section id="strategy" className="sec scene" data-snap>
        <div className="wrap inner">
          {head("03", "STRATEGY", "exit", "3")}
          <Split text="Rules in, emotion out." />
          <p className="lede reveal">Rho trades on fixed rules, never on gut feeling. Nemotron supervises.</p>

          <div className="nemo reveal">
            <div className="nemo-left">
              <div className="nemo-k"><span className="nv-dot" /> NVIDIA NEMOTRON · THE SUPERVISOR</div>
              <h3 className="nemo-h">The rules trade. Nemotron watches.</h3>
              <p>Nemotron never places a trade. Its agents watch the gate, test fixes, and draft a proposal. Anything
                that adds risk waits for your approval.</p>
              <div className="nemo-tools">
                {["observe", "detect", "simulate", "list_actions", "check_action", "request_kill", "audit_log", "report_template"].map((t) => (
                  <code key={t}>{t}</code>
                ))}
              </div>
            </div>
            <dl className="nemo-facts">
              <div><dt><Icon name="chip" size={14} /> Model</dt><dd>Nemotron Super<span>120B parameters, 12B active</span></dd></div>
              <div><dt><Icon name="globe" size={14} /> Runs on</dt><dd>NVIDIA API<span>Brev as backup</span></dd></div>
              <div><dt><Icon name="route" size={14} /> Agents</dt><dd>1 + 4<span>orchestrator + four agents</span></dd></div>
              <div><dt><Icon name="lock" size={14} /> On its own</dt><dd>Kill only<span>the rest needs you</span></dd></div>
            </dl>
          </div>
        </div>
      </section>

      {/* ---------- supervisor: pinned scroll drive ---------- */}
      <Drive stops={STOPS} />

      {/* ---------- safety ---------- */}
      <section id="safety" className="sec scene" data-snap>
        <div className="wrap inner">
          {head("05", "SAFETY", "stop")}
          <Split text="Rules of the road." />
          <p className="lede reveal">The supervisor can only make the system safer on its own. A person makes every other call.</p>
          <div className="rules reveal">
            <div className="rules-col">
              <h4 className="ok"><Icon name="check" size={16} /> It may</h4>
              <ul>
                <li>Request a kill, which only makes things safer</li>
                <li>Propose a config change for a human to approve</li>
                <li>Read status, log, and replay data</li>
                <li>Write proposals and incident reports</li>
              </ul>
            </div>
            <div className="rules-col">
              <h4 className="no"><Icon name="x" size={16} /> It may never</h4>
              <ul>
                <li>Arm the gate</li>
                <li>Clear a latched trip</li>
                <li>Change a hardware limit</li>
                <li>Send anything that raises exposure</li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* ---------- final CTA ---------- */}
      <section className="finale" data-snap>
        <div className="end-road"><WindingRoad id="end-road" d={END_ROAD} height={170} dur={20} /></div>
        <div className="wrap end-inner">
          <div className="mono-tag center reveal">[ 06 / NEXT EXIT ]</div>
          <Split text="Take it for a drive." />
          <p className="lede reveal">Inject a gate trip, run the supervisor, and decide the route yourself.</p>
          <button className="btn primary lg reveal" onClick={onLaunch}><Icon name="bolt" size={16} /> Run ρ in your browser</button>
        </div>
      </section>

      <footer className="foot">
        <div className="wrap foot-inner">
          <span className="foot-brand">Rho to Riches</span>
          <span>All market data and activity are simulated. Not investment advice.</span>
        </div>
      </footer>
    </div>
  );
}
