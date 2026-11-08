import { useEffect, useReducer, useState } from "react";
import ReactMarkdown from "react-markdown";
import { runSupervisor, decide, inject, health, type Ev, type ToolEvent, type Proposal } from "./api";
import ToolCard from "./components/ToolCard";
import ProposalView from "./components/ProposalView";
import Road, { type Stop } from "./components/Road";
import LiveStrip from "./components/LiveStrip";
import Icon from "./components/Icons";
import Nav from "./components/Nav";
import { downloadReportPdf } from "./report";

const AGENTS = [
  { id: "monitor", label: "Monitor", job: "Reads status 0x20 / log 0x21" },
  { id: "stress_test", label: "Stress Test", job: "Simulates on seeds 0–79" },
  { id: "proposal", label: "Proposal", job: "Drafts fixes, checks hard rules" },
  { id: "human", label: "Human", job: "Engineer accepts or denies" },
  { id: "post_mortem", label: "Post-Mortem", job: "Report from the audit log" },
];

type S = {
  stage: string; model: string; adapter: string;
  agents: Record<string, { status: "idle" | "running" | "done"; summary?: string }>;
  tools: ToolEvent[]; proposal?: Proposal; decision?: any; report?: string;
  healthy?: boolean; error?: string;
};
const init: S = { stage: "idle", model: "", adapter: "", agents: {}, tools: [] };

function reducer(s: S, e: Ev | { event: "reset" }): S {
  const d = (e as Ev).data;
  switch (e.event) {
    case "reset": return { ...init, model: s.model, adapter: s.adapter };
    case "status": return { ...s, stage: d.stage, model: d.model ?? s.model, adapter: d.adapter ?? s.adapter };
    case "agent_start": return { ...s, agents: { ...s.agents, [d.agent]: { status: "running" } } };
    case "agent_done": {
      let summary: string = d.summary || "";
      try { const j = JSON.parse(summary); if (j.recommended) summary = `Recommends ${j.recommended}. ${j.rationale ?? ""}`; } catch { /* text */ }
      summary = summary.replace(/[#*`]+/g, "").replace(/\s+/g, " ").trim();
      if (summary.length > 220) summary = summary.slice(0, 217).trimEnd() + "…";
      return { ...s, agents: { ...s.agents, [d.agent]: { status: "done", summary } } };
    }
    case "tool_call": return { ...s, tools: [...s.tools, d] };
    case "tool_result": return { ...s, tools: s.tools.map((t) => (t.id === d.id ? { ...t, ...d } : t)) };
    case "proposal": return { ...s, proposal: d };
    case "decision":
      return { ...s, decision: d, agents: { ...s.agents, human: { status: "done", summary: `${d.accepted ? "Accepted" : "Denied"} ${d.action_id} · ${d.by}` } } };
    case "final": return d.healthy ? { ...s, stage: "done", healthy: true } : { ...s, stage: "done", report: d.report };
    case "error": return { ...s, stage: "error", error: d.message };
    default: return s;
  }
}

export default function Desk({ onHome }: { onHome: () => void }) {
  const [s, dispatch] = useReducer(reducer, init);
  const [h, setH] = useState<any>();
  const [by, setBy] = useState("Anagha");
  const [flash, setFlash] = useState("");
  const busy = !["idle", "done", "error", "awaiting_human"].includes(s.stage);

  useEffect(() => { health().then(setH).catch(() => setH(null)); }, []);

  const go = async () => {
    dispatch({ event: "reset" });
    dispatch({ event: "status", data: { stage: "starting" } });
    try { await runSupervisor(dispatch); } catch (err) { dispatch({ event: "error", data: { message: String(err) } }); }
  };
  const onDecide = async (accept: boolean, actionId?: string) => {
    if (!s.proposal) return;
    dispatch({ event: "status", data: { stage: "applying" } });
    try { await decide(s.proposal.id, accept, by || "engineer", dispatch, actionId); }
    catch (err) { dispatch({ event: "error", data: { message: String(err) } }); }
  };
  const fire = async (kind: string, label: string) => {
    await inject(kind);
    if (kind === "reset") dispatch({ event: "reset" });
    setFlash(label);
    setTimeout(() => setFlash(""), 1800);
  };

  const stops: Stop[] = AGENTS.map((a) => {
    const status = a.id === "human"
      ? (s.decision ? "done" : s.stage === "awaiting_human" ? "running" : "idle")
      : s.agents[a.id]?.status ?? "idle";
    return { ...a, status, finding: s.agents[a.id]?.summary };
  });
  const isMock = (s.adapter || h?.adapter || "").startsWith("mock");

  const stageLabel: Record<string, string> = {
    idle: "Idle", starting: "Starting", monitor: "Monitoring", stress_test: "Stress testing", proposal: "Drafting proposal",
    awaiting_human: "Awaiting your decision", applying: "Applying", post_mortem: "Writing post-mortem", done: "Complete", error: "Error",
  };

  return (
    <div className="app desk">
      <Nav mode="desk" onHome={onHome}>
        <div className="chips">
          {h ? <>
            <span className="chip"><i className="live-dot" />{s.adapter || h.adapter}</span>
            <span className="chip mono">{(s.model || h.model || "").replace(/^nvidia\//, "")}</span>
            {h.mock && <span className="chip warn">mock LLM</span>}
          </> : <span className="chip bad">backend offline</span>}
        </div>
      </Nav>

      {isMock && <div className="mockbar"><Icon name="flask" size={13} /> Mock adapter · numbers are random and not a result</div>}

      <LiveStrip />

      <section className="controls">
        <div className="ctl-group">
          <span className="ctl-label">Scenario</span>
          <div className="seg">
            <button disabled={busy} onClick={() => fire("gate_trip", "Gate trip injected")}><Icon name="bolt" size={14} /> Gate trip</button>
            <button disabled={busy} onClick={() => fire("stale_feed", "Stale feed injected")}><Icon name="clock" size={14} /> Stale feed</button>
            <button disabled={busy} onClick={() => fire("reset", "System reset")}><Icon name="route" size={14} /> Reset</button>
          </div>
          {flash && <span className="flash"><Icon name="check" size={13} /> {flash}</span>}
        </div>
        <div className="ctl-group">
          <label className="ctl-label">Engineer <input className="by" value={by} onChange={(e) => setBy(e.target.value)} /></label>
          <button className="btn primary run-sup" onClick={go} disabled={busy}>
            {busy ? <><span className="spin" /> Running</> : <><Icon name="pulse" size={15} /> Run supervisor</>}
          </button>
        </div>
      </section>

      <section className="panel2">
        <header className="ph">
          <span className="ph-k">[ SUPERVISOR LOOP ]</span>
          <span className={`ph-stage s-${s.stage}`}><i />{stageLabel[s.stage] ?? s.stage}</span>
        </header>
        <Road stops={stops} stage={s.stage} healthy={s.healthy} />
        {s.healthy && <div className="note ok"><Icon name="check" size={14} /><span>No incidents detected. Inject a scenario to see the loop.</span></div>}
        {s.error && <div className="note bad"><Icon name="x" size={14} /><span>{s.error}</span></div>}
      </section>

      <main className="grid">
        <section className="panel2">
          <header className="ph">
            <span className="ph-k">[ TOOL CALLS ]</span>
            <span className="ph-meta">{s.tools.length} calls · Nemotron</span>
          </header>
          <div className="feed">
            {s.tools.length === 0 && <p className="empty">Every tool call Nemotron makes streams here, with its arguments and result.</p>}
            {s.tools.map((t) => <ToolCard key={t.id} t={t} />)}
          </div>
        </section>

        <section className="panel2">
          <header className="ph">
            <span className="ph-k">[ PROPOSAL ]</span>
            <span className="ph-meta">the engineer has the final say</span>
          </header>
          {!s.proposal && <p className="empty">{busy ? "The agents are working…" : "Inject a scenario, then run the supervisor."}</p>}
          {s.proposal && <ProposalView p={s.proposal} decided={!!s.decision} onDecide={onDecide} busy={busy} />}
          {s.decision && (
            <div className={`note ${s.decision.result?.ok ? "ok" : "bad"}`}>
              <Icon name={s.decision.accepted ? "check" : "x"} size={14} /><span>{s.decision.accepted ? "Accepted" : "Denied"} by {s.decision.by}. {s.decision.result?.reason}</span>
            </div>
          )}
          {s.report && <>
            <header className="ph sub"><span className="ph-k">[ POST-MORTEM ]</span><span className="ph-meta">from the hash-chained audit log</span></header>
            <div className="md"><ReactMarkdown>{s.report}</ReactMarkdown></div>
          </>}
        </section>
      </main>

      <section className="panel2 report-dl">
        <header className="ph">
          <span className="ph-k">[ REPORT ]</span>
          <span className="ph-meta">{s.report ? "ready" : s.decision ? "writing post-mortem…" : "available after the post-mortem"}</span>
        </header>
        <div className="rdl-row">
          <span className="rdl-ico"><Icon name="file" size={20} /></span>
          <div className="rdl-txt">
            <b>Incident report (PDF)</b>
            <span>Incident, options with hard-rule checks, your decision, the post-mortem and the hash-chained audit log, ready to save as a PDF.</span>
          </div>
          <button className="btn primary" disabled={!s.report}
            onClick={() => downloadReportPdf({ model: s.model, adapter: s.adapter, proposal: s.proposal, decision: s.decision, report: s.report, tools: s.tools, agents: s.agents }, by)}>
            <Icon name="download" size={15} /> Download PDF
          </button>
        </div>
      </section>

      <footer className="desk-foot">All results simulated · The supervisor can request a kill, but never arm the gate, clear a trip, or change a hardware limit.</footer>
    </div>
  );
}
