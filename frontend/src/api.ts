// Supervisor API: SSE over fetch for runs and decisions, plain JSON for the rest.

export type ToolEvent = {
  id: string; agent: string; name: string; args: Record<string, unknown>;
  result?: any; ms?: number;
};
export type Check = { pass: boolean; reason: string };
export type Split = { evaluated: boolean; seeds?: number; [k: string]: any };
export type Outcome = { metrics: any; in_sample: Split; out_of_sample: Split; source: string };
export type Candidate = {
  action: { id: string; label: string; params: any; reduces_risk: boolean };
  check: Check; outcome: Outcome | null;
};
export type Proposal = {
  id: string; adapter: string; incident: any; state: any; baseline: Outcome | null;
  candidates: Candidate[]; recommended: string | null; ranking: string[]; rationale: string;
  auto_actions: { action: any; result: any; reason: string }[]; invariants: string[]; label: string;
};
export type Ev = { event: string; data: any };

async function stream(url: string, body: unknown, onEvent: (e: Ev) => void) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const ev = /^event: (.*)$/m.exec(chunk)?.[1];
      const data = /^data: (.*)$/m.exec(chunk)?.[1];
      if (ev && data) onEvent({ event: ev, data: JSON.parse(data) });
    }
  }
}

export const runSupervisor = (on: (e: Ev) => void) => stream("/api/supervisor/run", {}, on);
export const decide = (pid: string, accept: boolean, by: string, on: (e: Ev) => void, action_id?: string) =>
  stream(`/api/proposals/${pid}/decide`, { accept, by, action_id }, on);
export const inject = (kind: string) =>
  fetch("/api/inject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind }) }).then((r) => r.json());
export const health = () => fetch("/api/health").then((r) => r.json());
