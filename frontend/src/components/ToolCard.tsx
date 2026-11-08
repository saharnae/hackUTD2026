import type { ToolEvent } from "../api";

import Icon from "./Icons";

const ICON: Record<string, string> = {
  observe: "eye", detect: "pulse", simulate: "flask", list_actions: "route", check_action: "shield",
  request_kill: "stop", audit_log: "file", report_template: "file",
};

function summary(t: ToolEvent): string | null {
  const r = t.result;
  if (r === undefined) return null;
  if (r?.error) return `error: ${r.error}`;
  switch (t.name) {
    case "observe": return `gate ${r.gate?.latched ? "LATCHED (" + r.gate.trip_reason + ")" : "ok"} · feed ${r.feed_age_ms}ms · bad frames ${r.bad_frames_1m}`;
    case "detect": return r.length ? r.map((i: any) => `${i.kind} (sev ${i.severity})`).join(", ") : "no incidents";
    case "simulate": return `in-sample DD ${r.in_sample?.mean_max_drawdown} · trips ${r.in_sample?.mean_gate_trips} · OOS held out`;
    case "list_actions": return `${r.length} candidate fixes`;
    case "check_action": return r.pass ? "passed the hard-rule check" : r.reason;
    case "request_kill": return r.ok ? "kill sent (0x15)" : r.reason;
    case "audit_log": return `${r.length} hash-chained entries`;
    default: return null;
  }
}

export default function ToolCard({ t }: { t: ToolEvent }) {
  const pending = t.result === undefined;
  const bad = !pending && (t.result?.error || (t.name === "check_action" && !t.result.pass));
  return (
    <details className={`tool ${pending ? "pending" : bad ? "err" : ""}`}>
      <summary>
        <span className="tool-icon"><Icon name={ICON[t.name] ?? "chip"} size={14} /></span>
        <span className="tool-name">{t.name}</span>
        <span className="agent-tag">{t.agent.replace("_", " ")}</span>
        <span className="tool-args">{Object.entries(t.args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ")}</span>
        <span className="tool-ms">{pending ? <span className="spin" /> : `${t.ms}ms`}</span>
        {summary(t) && <div className="tool-sum">{summary(t)}</div>}
      </summary>
      {!pending && <pre>{JSON.stringify(t.result, null, 2)}</pre>}
    </details>
  );
}
