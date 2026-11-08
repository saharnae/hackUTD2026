import type { Proposal, Split } from "../api";
import Icon from "./Icons";

const num = (v: any, d = 0) => (typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: d }) : "–");
const split = (x?: Split) =>
  !x ? "–" : !x.evaluated ? <span className="muted">held out</span>
    : <><span className="mono">{num(x.mean_max_drawdown)}</span> <span className="muted">· {num(x.mean_gate_trips, 2)} trips</span></>;

export default function ProposalView({ p, decided, onDecide, busy }: {
  p: Proposal; decided: boolean; busy: boolean; onDecide: (accept: boolean, actionId?: string) => void;
}) {
  const inc = p.incident;
  const rec = p.candidates.find((c) => c.action.id === p.recommended);
  return (
    <div className="proposal">
      <div className={`incident sev${inc.severity}`}>
        <span className="inc-ico"><Icon name="bolt" size={18} /></span>
        <div>
          <div className="inc-title">{inc.kind.replace(/_/g, " ")} <span className="sev">SEVERITY {inc.severity}</span></div>
          <div className="inc-detail">{Object.entries(inc.detail).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")}</div>
        </div>
      </div>

      {p.auto_actions.map((a, i) => (
        <div key={i} className="note kill"><Icon name="stop" size={14} /><span>The supervisor requested a <b>kill</b> on its own, which is allowed because it only makes things safer. {a.result.reason}</span></div>
      ))}

      <table className="ptable">
        <thead>
          <tr><th>Option</th><th>Hard rules</th><th>In-sample DD<span className="th-sub">seeds 0–79</span></th><th>Out-of-sample<span className="th-sub">final eval only</span></th></tr>
        </thead>
        <tbody>
          <tr className="base"><td>Do nothing (baseline)</td><td>–</td><td>{split(p.baseline?.in_sample)}</td><td>{split(p.baseline?.out_of_sample)}</td></tr>
          {p.candidates.map((c) => (
            <tr key={c.action.id} className={c.action.id === p.recommended ? "rec" : !c.check.pass ? "refused" : ""}>
              <td>
                {c.action.id === p.recommended && <span className="rec-tag">Recommended</span>}
                <div className="opt-label">{c.action.label}</div>
                {(c.action.reduces_risk || p.auto_actions.some((a) => a.action.id === c.action.id && a.result?.ok)) && (
                  <div className="opt-tags">
                    {c.action.reduces_risk && <span className="tag">safer-only</span>}
                    {p.auto_actions.some((a) => a.action.id === c.action.id && a.result?.ok) && <span className="tag applied">already applied</span>}
                  </div>
                )}
              </td>
              <td className={c.check.pass ? "pass" : "fail"} title={c.check.reason}>
                {c.check.pass ? <><Icon name="check" size={13} /> Pass</> : <><Icon name="x" size={13} /> {c.check.reason.replace(/^Refused[^:]*:\s*/, "")}</>}
              </td>
              <td>{split(c.outcome?.in_sample)}</td>
              <td>{split(c.outcome?.out_of_sample)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="muted small">DD = mean max drawdown ($) · {p.baseline?.source}</div>

      {rec && <p className="rationale"><b>Recommendation:</b> {rec.action.label}. {p.rationale}</p>}

      <details className="inv"><summary>Hard rules the supervisor cannot break</summary>
        <ul>{p.invariants.map((r) => <li key={r}>{r}</li>)}</ul></details>

      {!decided && (
        <div className="decide">
          <button className="btn primary" disabled={busy || !rec} onClick={() => onDecide(true)}><Icon name="check" size={15} /> Accept</button>
          <button className="btn ghost" disabled={busy} onClick={() => onDecide(false)}><Icon name="x" size={15} /> Deny</button>
        </div>
      )}
    </div>
  );
}
