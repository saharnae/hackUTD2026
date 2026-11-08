// The supervisor loop as a slim pipeline: a lane line, small sign badges for each stage,
// and the ρ marker moving along as the agents work. Findings sit under each stage.
import Icon from "./Icons";

export type Stop = { id: string; label: string; job: string; status: "idle" | "running" | "done"; finding?: string };

const STAGE_POS: Record<string, number> = {
  idle: -0.5, starting: -0.25, monitor: 0, stress_test: 1, proposal: 2,
  awaiting_human: 3, applying: 3.5, post_mortem: 4, done: 4.6, error: -0.5,
};
const ICON: Record<string, string> = { monitor: "eye", stress_test: "flask", proposal: "route", human: "user", post_mortem: "file" };

export default function Road({ stops, stage, healthy }: { stops: Stop[]; stage: string; healthy?: boolean }) {
  const n = stops.length;
  const pos = healthy ? 0 : STAGE_POS[stage] ?? -0.5;
  const pct = (pos / (n - 1)) * 100;                   // % along the lane, which runs centre-to-centre
  const moving = !["idle", "done", "error", "awaiting_human"].includes(stage);
  const doneUpTo = stops.reduce((a, s, i) => (s.status === "done" ? i : a), -1);
  const fill = doneUpTo < 0 ? 0 : (doneUpTo / (n - 1)) * 100;

  return (
    <div className="pipe">
      <div className={`pipe-lane ${moving ? "moving" : ""}`} style={{ left: `${50 / n}%`, right: `${50 / n}%` }}>
        <span className="pipe-fill" style={{ width: `${Math.max(0, fill)}%` }} />
        <span className="pipe-car" style={{ left: `${Math.max(-6, Math.min(106, pct))}%` }}>
          <svg width="22" height="24" viewBox="0 0 100 108" aria-hidden>
            <path d="M50 4 C66 12 82 12 96 6 C100 40 92 78 50 104 C8 78 0 40 4 6 C18 12 34 12 50 4Z" fill="#1f4fbf" stroke="#fff" strokeWidth="7" />
            <path d="M8 8 C22 14 36 14 50 7 C64 14 78 14 92 8 L91 30 L9 30Z" fill="#c8102e" />
            <text x="50" y="84" textAnchor="middle" fontFamily="Inter, sans-serif" fontWeight="600" fontSize="58" fill="#fff">ρ</text>
          </svg>
        </span>
      </div>
      <ol className="pipe-stops" style={{ gridTemplateColumns: `repeat(${n}, 1fr)` }}>
        {stops.map((s, i) => (
          <li key={s.id} className={`ps ${s.status}`}>
            <div className="ps-node">
              {s.id === "human"
                ? <span className="badge-stop">STOP</span>
                : <span className="badge-exit"><em>EXIT</em>{i + 1}</span>}
            </div>
            <div className="ps-head">
              <Icon name={ICON[s.id] ?? "pulse"} size={14} />
              <span className="ps-name">{s.label}</span>
              <span className="ps-state">{s.status === "done" ? "done" : s.status === "running" ? (s.id === "human" ? "waiting" : "running") : ""}</span>
            </div>
            <div className="ps-job">{s.job}</div>
            {s.finding && <div className="ps-find">{s.finding}</div>}
          </li>
        ))}
      </ol>
    </div>
  );
}
