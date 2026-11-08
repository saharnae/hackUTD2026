// Builds a printable incident report from the desk's run and opens the browser's
// "Save as PDF" dialog. No extra dependencies: a hidden iframe + window.print().
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import type { Proposal, ToolEvent } from "./api";

export type RunSnapshot = {
  model: string; adapter: string; proposal?: Proposal; decision?: any; report?: string;
  tools: ToolEvent[]; agents: Record<string, { status: string; summary?: string }>;
};

const esc = (v: unknown) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
const num = (v: any, d = 0) => (typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: d }) : "–");
const split = (x: any) => (!x ? "–" : !x.evaluated ? "held out" : `${num(x.mean_max_drawdown)} · ${num(x.mean_gate_trips, 2)} trips`);

const AGENTS: [string, string][] = [
  ["monitor", "Monitor"], ["stress_test", "Stress Test"], ["proposal", "Proposal"], ["human", "Human"], ["post_mortem", "Post-Mortem"],
];

const SHIELD = `<svg width="30" height="32" viewBox="0 0 100 108"><path d="M50 4 C66 12 82 12 96 6 C100 40 92 78 50 104 C8 78 0 40 4 6 C18 12 34 12 50 4Z" fill="#1f4fbf" stroke="#111" stroke-width="5"/><path d="M8 8 C22 14 36 14 50 7 C64 14 78 14 92 8 L91 30 L9 30Z" fill="#c8102e"/><text x="50" y="84" text-anchor="middle" font-family="Inter, sans-serif" font-weight="600" font-size="58" fill="#fff">ρ</text></svg>`;

const CSS = `
@page { size: letter; margin: 16mm 15mm 18mm; }
* { box-sizing: border-box; }
body { font: 10.5pt/1.5 Inter, -apple-system, "Segoe UI", sans-serif; color: #15181b; margin: 0; }
.mono, code, pre { font-family: "JetBrains Mono", ui-monospace, Menlo, monospace; }
header { display: flex; align-items: center; gap: 12px; border-bottom: 2px solid #15181b; padding-bottom: 10px; margin-bottom: 14px; }
header h1 { font: 800 19pt/1.1 "Bricolage Grotesque", Inter, sans-serif; margin: 0; letter-spacing: -.01em; }
header .sub { font-size: 8.5pt; color: #5b636b; letter-spacing: .08em; text-transform: uppercase; }
header .right { margin-left: auto; text-align: right; font-size: 8.5pt; color: #5b636b; }
.meta { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; border: 1px solid #d5dade; border-radius: 6px; margin-bottom: 16px; }
.meta div { padding: 7px 10px; border-right: 1px solid #d5dade; }
.meta div:last-child { border-right: 0; }
.meta b { display: block; font-size: 7.5pt; color: #6b737b; letter-spacing: .08em; text-transform: uppercase; font-weight: 600; }
.meta span { font-size: 9.5pt; word-break: break-word; }
h2 { font: 700 11.5pt/1.2 Inter, sans-serif; margin: 18px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #d5dade; text-transform: uppercase; letter-spacing: .06em; }
.incident { border-left: 4px solid #c8102e; background: #fbf1f2; padding: 8px 12px; border-radius: 0 6px 6px 0; }
.incident .k { font-weight: 700; text-transform: capitalize; }
.sev { font-size: 8pt; font-weight: 700; color: #c8102e; margin-left: 6px; letter-spacing: .06em; }
table { width: 100%; border-collapse: collapse; font-size: 9pt; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e3e6e9; vertical-align: top; }
th { font-size: 7.5pt; text-transform: uppercase; letter-spacing: .07em; color: #5b636b; border-bottom: 1.5px solid #15181b; }
tr.rec td { background: #eef8ef; }
tr.rec td:first-child { box-shadow: inset 3px 0 0 #00a004; }
.pass { color: #0a7a14; font-weight: 600; } .fail { color: #b00c25; }
.tag { display: inline-block; font-size: 7pt; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; border: 1px solid currentColor; border-radius: 3px; padding: 0 4px; margin-right: 4px; color: #0a7a14; }
.tag.rec { color: #15181b; }
.decision { border: 1px solid #d5dade; border-radius: 6px; padding: 8px 12px; }
.decision.ok { border-left: 4px solid #00a004; } .decision.bad { border-left: 4px solid #c8102e; }
ol.loop { margin: 0; padding-left: 18px; } ol.loop li { margin-bottom: 4px; }
.md h1, .md h2, .md h3 { text-transform: none; letter-spacing: 0; border: 0; font-size: 11pt; margin: 12px 0 4px; }
.md p, .md li { margin: 3px 0; } .md code { font-size: 8.5pt; background: #f1f3f5; padding: 0 3px; border-radius: 3px; }
.audit td { font-size: 8pt; } .audit td.h { white-space: nowrap; }
.small { font-size: 8pt; color: #6b737b; }
.avoid { break-inside: avoid; }
footer { margin-top: 20px; padding-top: 8px; border-top: 1px solid #d5dade; font-size: 7.5pt; color: #6b737b; letter-spacing: .05em; text-transform: uppercase; }
`;

export function buildReportHtml(r: RunSnapshot, by: string): { html: string; title: string } {
  const p = r.proposal, inc = p?.incident, now = new Date();
  const stamp = now.toISOString().slice(0, 16).replace("T", " ");
  const title = `rho-to-riches-incident-${p?.id ?? now.toISOString().slice(0, 10)}`;
  const audit = [...r.tools].reverse().find((t) => t.name === "audit_log" && Array.isArray(t.result))?.result as any[] | undefined;
  const md = r.report ? renderToStaticMarkup(createElement(ReactMarkdown, null, r.report)) : "";

  const rows = p ? [
    `<tr><td>Do nothing (baseline)</td><td>–</td><td class="mono">${esc(split(p.baseline?.in_sample))}</td><td class="mono">${esc(split(p.baseline?.out_of_sample))}</td></tr>`,
    ...p.candidates.map((c) => `<tr class="${c.action.id === p.recommended ? "rec" : ""}">
      <td>${c.action.id === p.recommended ? '<span class="tag rec">Recommended</span>' : ""}${c.action.reduces_risk ? '<span class="tag">safer-only</span>' : ""}<div>${esc(c.action.label)}</div></td>
      <td class="${c.check.pass ? "pass" : "fail"}">${c.check.pass ? "Pass" : esc(c.check.reason.replace(/^Refused[^:]*:\s*/, ""))}</td>
      <td class="mono">${esc(split(c.outcome?.in_sample))}</td><td class="mono">${esc(split(c.outcome?.out_of_sample))}</td></tr>`),
  ].join("") : "";

  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title><style>${CSS}</style></head><body>
<header>${SHIELD}<div><h1>Incident Report</h1><div class="sub">Rho to Riches · Nemotron supervisor</div></div>
<div class="right">${esc(stamp)}<br>${p ? `Proposal <span class="mono">${esc(p.id)}</span>` : ""}</div></header>

<div class="meta">
  <div><b>Adapter</b><span class="mono">${esc(r.adapter || "–")}</span></div>
  <div><b>Model</b><span class="mono">${esc((r.model || "–").replace(/^nvidia\//, ""))}</span></div>
  <div><b>Engineer</b><span>${esc(r.decision?.by ?? by)}</span></div>
  <div><b>Tool calls</b><span>${r.tools.length}</span></div>
</div>

${inc ? `<h2>Incident</h2><div class="incident avoid"><span class="k">${esc(String(inc.kind).replace(/_/g, " "))}</span><span class="sev">SEVERITY ${esc(inc.severity)}</span>
<div class="small">${esc(Object.entries(inc.detail ?? {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · "))}</div></div>` : ""}

${p?.auto_actions?.length ? `<p class="small">Automatic safe action: ${p.auto_actions.map((a) => esc(`${a.action?.label ?? a.action?.id} — ${a.result?.reason ?? a.reason}`)).join("; ")}</p>` : ""}

${p ? `<h2>Options considered</h2><table class="avoid"><thead><tr><th>Option</th><th>Hard rules</th><th>In-sample DD (seeds 0–79)</th><th>Out-of-sample</th></tr></thead><tbody>${rows}</tbody></table>
<p class="small">DD = mean max drawdown ($) · ${esc(p.baseline?.source ?? "")}</p>
${p.rationale ? `<p><b>Recommendation:</b> ${esc(p.rationale)}</p>` : ""}` : ""}

${r.decision ? `<h2>Human decision</h2><div class="decision avoid ${r.decision.result?.ok ? "ok" : "bad"}"><b>${r.decision.accepted ? "Accepted" : "Denied"}</b>
<span class="mono">${esc(r.decision.action_id)}</span> by ${esc(r.decision.by)}. ${esc(r.decision.result?.reason ?? "")}</div>` : ""}

<h2>Supervisor loop</h2><ol class="loop">${AGENTS.map(([id, label]) => `<li><b>${label}.</b> ${esc(r.agents[id]?.summary ?? (id === "human" && r.decision ? `${r.decision.accepted ? "Accepted" : "Denied"} by ${r.decision.by}` : "—"))}</li>`).join("")}</ol>

${md ? `<h2>Post-mortem</h2><div class="md">${md}</div>` : ""}

${audit?.length ? `<h2>Audit log (hash-chained)</h2><table class="audit"><thead><tr><th>t (s)</th><th>Event</th><th>Hash</th><th>Prev</th></tr></thead><tbody>
${audit.map((e) => `<tr><td class="mono">${esc(e.t)}</td><td>${esc(e.event)}</td><td class="mono h">${esc(e.hash)}</td><td class="mono h">${esc(e.prev)}</td></tr>`).join("")}</tbody></table>` : ""}

${p?.invariants?.length ? `<h2>Hard rules</h2><ul class="small">${p.invariants.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}

<footer>All results simulated · The supervisor can request a kill, but never arm the gate, clear a trip, or change a hardware limit.</footer>
</body></html>`;
  return { html, title };
}

/** Opens the print dialog for the report; choose "Save as PDF" as the destination. */
export function downloadReportPdf(r: RunSnapshot, by: string) {
  const { html, title } = buildReportHtml(r, by);
  const f = document.createElement("iframe");
  f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
  document.body.appendChild(f);
  const doc = f.contentDocument!;
  doc.open(); doc.write(html); doc.close();
  const prev = document.title;
  document.title = title;                       // browsers use this as the default PDF file name
  const done = () => { document.title = prev; setTimeout(() => f.remove(), 500); };
  f.contentWindow!.addEventListener("afterprint", done);
  setTimeout(() => { f.contentWindow!.focus(); f.contentWindow!.print(); }, 250);
  setTimeout(done, 60_000);
}
