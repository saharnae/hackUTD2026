"""SOFTWARE TWIN, NOT THE FPGA. Fallback that speaks the real UART frames so the backend and agents
can run without the board. SIMULATED arena: simulated market, simulated fills. Any timing it
reports is "twin timing, not FPGA latency".

Runs golden/engine.py (P1) over one seeded sim/options_model.py stream (seeds 0-79 only) and
  - emits 0x20 status frames every status period, and 0x21 order/fill log records,
  - accepts 0x12 config (slowpath/frames.py ConfigRegister rules, applied only while disarmed),
    0x15 kill (latches, safer only), 0x16 control, 0x14 hedge orders (through the gate),
  - validates and logs every other type, and recovers from bad frames exactly like
    slowpath/frames.py FrameDecoder (it IS that decoder).
Arming: ONLY the local switch (stdin "a" = SW9 rising edge, "d" = SW9 low, "c" = KEY0 clear while
ARM is low). No frame, no TCP client and no agent action can arm or clear a latch: frames go
through a separate channel that has no path to the switch.

    python tools/fake_fpga.py --seed 3 --hedge-band 500                 # TCP loopback server
    python tools/fake_fpga.py --seed 3 --hedge-band 500 --out-file twin.bin --switch a@60000

ASSUMPTIONS (TW, all TODO(FINAL)):
  TW1. Transport: TCP on 127.0.0.1, port 47420 (TODO(FINAL)), one client. The session starts when
       the client connects. --out-file instead writes the raw outbound frame bytes for replay
       (no pacing); --in-file frames are applied at t = 0 in that mode.
  TW2. Status period = 100 ms of twin time (FRAMES.md reset value; control op 0x02 changes it).
       Also sent at once on op 0x01 and on every new latch (so the trip reason is reported).
  TW3. The 0x20 frame has NO sequence field (FRAMES.md, fpga/io/status_tx.sv). Its counters behave
       like the RTL: now_us u32 wraps, orders_sent (approved) and fills (fill reports with qty > 0)
       u16 wrap, frames_bad / config_rejects / log_drops u8 saturate. snapshot_id is the twin's
       u16 count of engine snapshots.
  TW4. last_latency_ns is FPGA latency (io_shock T0 -> first order); the twin cannot measure it and
       sends 0. Per-message processing time is reported only in console JSON as twin_ns.
  TW5. 0x21 records: one per gate decision (rec 1; rejected and blocked ones too, gate_eval is pure so
       it is evaluated for the record even while blocked, like the RTL core) and one per resolved
       order (rec 2, qty 0 on a miss); liquidation fills from a trip/kill flatten are rec 2 too.
       Latches with no order (R03, R13, R15) appear in the next status frame (trip_reason).
       log_drops stays 0: the twin's transport has no 32-deep FIFO.
  TW6. R14 uses the engine's own simulated heartbeats (engine E14). Host 0x11 frames are validated
       (hb_seq must change) and counted in console JSON only. 0x10 snapshots, 0x13 load blocks and
       0x17 feed words are validated and logged but not applied (the engine builds its own
       snapshots and the seeded stream is the feed). 0x20/0x21 from the host are logged and ignored.
  TW7. A KILL frame sets the engine kill input; it stays set for the session, so KEY0 cannot clear
       it in the twin (stricter than the board, where KEY0 clears a kill-frame trip).
  TW8. Live pacing: --speed scales twin time to wall time (default 0.01: a 600 ms session takes
       60 s). After the stream ends the twin keeps sending status (now_us advancing) until stopped.
  TW9. hedge_band_shares has no confirmed default: --hedge-band is required (200-1000).
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "golden"), os.path.join(ROOT, "sim"), os.path.join(ROOT, "slowpath"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine as E  # noqa: E402
import frames as fr  # noqa: E402
import gen_status_frame as gsf  # noqa: E402  (existing 0x20 payload packer; not edited)
import golden as g  # noqa: E402
import options_model as om  # noqa: E402

BANNER = "SOFTWARE TWIN, NOT THE FPGA"
SOURCE = "software_twin"
TIMING = "twin timing, not FPGA latency"
DEFAULT_PORT = 47420                  # TW1  TODO(FINAL)
STATUS_PERIOD_US = 100_000            # TW2  TODO(FINAL)
SEED_RANGE = (0, 79)                  # in-sample only; 80-99 are reserved for the one-shot final eval
ARM_SWITCH = "SW9 rising edge after boot (stdin 'a')"   # matches backend/config.py ARM_SWITCH  TODO(FINAL)
LATCHING = (3, 12, 13, 14, 15)


class SeedRefused(ValueError):
    pass


def check_seed(seed):
    if not SEED_RANGE[0] <= seed <= SEED_RANGE[1]:
        raise SeedRefused(f"seed {seed} refused: the twin runs only seeds {SEED_RANGE[0]}-{SEED_RANGE[1]}")
    return seed


def say(msg, out=None):
    print(f"[TWIN] {msg}", file=out or sys.stdout, flush=True)


def jline(kind, out=None, **kw):
    doc = {"source": SOURCE, "timing": TIMING, "kind": kind, **kw}
    say(json.dumps(doc, sort_keys=True), out)
    return doc


# ---------------------------------------------------------------- 0x21 record (FRAMES.md, 20 bytes)
def log_payload(rec_type, ts_us, is_hedge, side, contract, qty, price, reason=0, flags=0, fail_mask=0,
                client_id=0, tokens=0, orders_in_event=0):
    instr = (int(is_hedge) << 7) | (int(side) << 4) | (contract & 7)
    p = (bytes([rec_type]) + (int(ts_us) & 0xFFFFFFFF).to_bytes(4, "big") + bytes([instr])
         + (qty & 0xFFFF).to_bytes(2, "big") + (price & 0x3FFFF).to_bytes(3, "big") + bytes([reason & 0xF, flags])
         + (fail_mask & 0x7FFF).to_bytes(2, "big") + (client_id & 0xFFFF).to_bytes(2, "big")
         + bytes([tokens & 0xFF, orders_in_event & 0xFF, 0]))
    assert len(p) == 20
    return p


def unpack_log(p):
    return {"rec_type": p[0], "ts_us": int.from_bytes(p[1:5], "big"), "is_hedge": p[5] >> 7, "side": (p[5] >> 4) & 1,
            "contract": p[5] & 7, "qty": int.from_bytes(p[6:8], "big"), "price": int.from_bytes(p[8:11], "big"),
            "reason": p[11], "flags": p[12], "fail_mask": int.from_bytes(p[13:15], "big"),
            "client_id": int.from_bytes(p[15:17], "big"), "tokens": p[17], "orders_in_event": p[18]}


F_APPROVED, F_REDUCE, F_BLOCKED, F_FROM_FRAME = 1, 2, 4, 8


# ---------------------------------------------------------------- engine subclass (engine.py not edited)
class _Log(list):
    """Drops the engine's ("armed", t) entries when its auto-arm was refused (it arms only via SW9)."""

    def __init__(self, eng):
        super().__init__()
        self._eng = eng

    def append(self, x):
        if x and x[0] == "armed" and not self._eng._armed:
            return
        super().append(x)


class TwinEngine(E.Engine):
    """P1 engine whose arming needs a local SW9 rising edge, and which reports every gate
    decision and fill to the twin for 0x21 records."""

    def __init__(self, params, twin):
        self._armed, self._edge_now, self._twin = False, False, twin
        super().__init__(params)
        self.log = _Log(self)
        self.orders_in_event = 0                            # the engine sets it on its first step

    @property
    def armed(self):
        return self._armed

    @armed.setter
    def armed(self, v):
        if not v:
            self._armed = False
        elif not self._armed and self._edge_now:      # only a SW9 rising edge, while the conditions hold
            self._armed, self._edge_now = True, False
            self._twin._on_armed()

    def send(self, now, od, q, purpose, target_t=None):
        pre = None
        if self.snap is not None and q is not None and self.S_q4 is not None:
            pre = g.gate_eval(self._gate_state(now, q), od)   # pure: same inputs the engine uses
        blocked = self.latched or not self.armed
        r = super().send(now, od, q, purpose, target_t)
        self._twin._on_decision(now, od, purpose, r, pre, blocked)
        return r

    def _resolve(self, o):
        n = len(self.fills)
        super()._resolve(o)
        got = self.fills[n:]
        qty = abs(got[0]["qty"]) if got else 0
        self._twin._on_fill(o["t_arrive"], o["od"], qty, o["purpose"])

    def _flatten(self, now):
        n = len(self.fills)
        super()._flatten(now)
        for f in self.fills[n:]:
            hedge = f["kind"] == "hedge"
            od = {"is_hedge": int(hedge), "side": 0 if f["qty"] > 0 else 1,
                  "contract": 0 if hedge else f["contract"], "price": f["price"]}
            self._twin._on_fill(now, od, abs(f["qty"]), "flatten")


# ---------------------------------------------------------------- the twin
class Twin:
    def __init__(self, seed, hedge_band, regime="normal", emit=None, out=None, status_period_us=STATUS_PERIOD_US,
                 quiet=False):
        check_seed(seed)                                    # before anything is built
        if regime not in om.REGIMES:
            raise ValueError(f"unknown regime {regime}")
        self.seed, self.regime, self.out, self.quiet = seed, regime, out, quiet
        self.emit_bytes = emit or (lambda b: None)
        self.sc = om.OptionsMarketScenario(seed, regime)
        self.cfg = fr.ConfigRegister()
        self.eng = TwinEngine(self._engine_params(self.cfg.params, hedge_band), self)
        self.decoder = fr.FrameDecoder()
        self.frames_in = queue.Queue()                      # host -> twin bytes (frame channel)
        self.local_in = queue.Queue()                       # local switch events ONLY (stdin / CLI)
        self.scripted_local = []                            # [(t_us, key)] for replay / tests
        self.scripted_frames = []                           # [(t_us, bytes)]
        self.status_period = status_period_us
        self.log_enabled = True
        self.arm_switch = False
        self.now = 0.0
        self.next_status = 0.0
        self.approved = 0
        self.fill_reports = 0
        self.frames_bad_extra = 0
        self.last_reject = 0
        self.snapshot_id, self._snap_ref = 0, None
        self.host_hb = {"valid": 0, "last_seq": None}
        self.ignored = {}
        self.records = []                                   # outbound (type, payload) in order
        self.ever_armed = False
        self._latched_seen = 0
        self.twin_ns_max, self.twin_ns_last = 0, 0
        self.pace = None                                    # callable(t_us) for live pacing

    @staticmethod
    def _engine_params(cp, hedge_band):
        return {"edge_threshold_ticks": cp["edge_threshold"] / 256, "exit_edge_ticks": cp["exit_edge"] / 256,
                "min_move_cents": cp["min_move"], "max_orders_per_event": cp["max_orders_per_event"],
                "size_cap_base": cp["size_cap_base"], "size_cap_max": cp["size_cap_max"],
                "time_stop_us": cp["time_stop_us"], "hedge_band_shares": hedge_band}

    # ------------------------------------------------------------ output
    def _send(self, ftype, payload):
        b = fr.encode(ftype, payload)                       # existing encoder, existing CRC
        self.records.append((ftype, bytes(payload)))
        self.emit_bytes(b)
        return b

    def _say(self, kind, **kw):
        if not self.quiet:
            jline(kind, self.out, **kw)

    def status_fields(self):
        e = self.eng
        snap_ok = (e.snap is not None and (self.now - e.snap_flip) <= g.STALE_US and e.last_seq is not None
                   and ((e.last_seq - e.ref_seq) & 0xFFFF) <= g.SYNC_MAX_BEHIND)
        book_ready = e.S_q4 is not None and all(e.opt_q)
        hb_ok = e.hb_count > 0 and (self.now - e.hb_last) <= g.HEARTBEAT_US
        state = (int(e.armed) | int(e.latched) << 1 | int(book_ready) << 2 | int(snap_ok) << 3 | int(hb_ok) << 4
                 | int(self.arm_switch) << 6 | int(e.kill) << 7)
        if e.snap is not None and e.S_q4 is not None:
            tn = e.theo_new()
            eq = 64 * e.cash + 25 * sum(p * x for p, x in zip(e.pos_conf, tn)) + 4 * e.sh_conf * e.S_q4
        else:
            eq = 64 * e.cash
        return dict(now_us=int(self.now) & 0xFFFFFFFF, state=state,
                    trip_reason=e.latch_reasons[0]["reason"] if e.latch_reasons else 0,
                    last_reject=self.last_reject, tokens=e.tokens, snapshot_id=self.snapshot_id & 0xFFFF,
                    ref_seq=e.ref_seq & 0xFFFF, feed_seq=(e.last_seq or 0) & 0xFFFF, S=e.S_q4 or 0, equity_x6400=eq,
                    orders_sent=self.approved & 0xFFFF, fills=self.fill_reports & 0xFFFF, last_latency_ns=0,  # TW4
                    frames_bad=min(255, self.decoder.bad_frames + self.frames_bad_extra),
                    config_rejects=min(255, self.cfg.config_rejects), log_drops=0)

    def send_status(self):
        f = self.status_fields()
        self._send(fr.T_STATUS, gsf.pack_payload(**f))
        self._say("status", seq_note="0x20 has no seq field (TW3)", twin_ns_last=self.twin_ns_last,
                  twin_ns_max=self.twin_ns_max, host_heartbeats=self.host_hb["valid"], **f)

    def _log(self, *a, **kw):
        if self.log_enabled:
            self._send(fr.T_LOG, log_payload(*a, **kw))

    # ------------------------------------------------------------ engine callbacks
    def _on_armed(self):
        self.ever_armed = True
        self._say("armed", t_us=self.now, by="local switch " + ARM_SWITCH)

    def _on_decision(self, now, od, purpose, r, pre, blocked):
        approved = r == 0
        reason = pre["reason"] if pre else 0
        if r > 0:
            reason = r
        flags = (F_APPROVED if approved else 0) | (F_REDUCE if pre and pre["reduce_only"] else 0) \
            | (F_BLOCKED if blocked else 0) | (F_FROM_FRAME if purpose == "host_hedge" else 0)
        if approved:
            self.approved += 1
        else:
            self.last_reject = reason
        self._log(1, now, od["is_hedge"], od["side"], od["contract"], od["qty"], od["price"], reason, flags,
                  pre["fail_mask"] if pre else 0, od.get("client_id", 0), self.eng.tokens, self.eng.orders_in_event)

    def _on_fill(self, t, od, qty, purpose):
        if qty > 0:
            self.fill_reports += 1
        self._log(2, t, od["is_hedge"], od["side"], od["contract"], qty, od["price"], 0, 0, 0, od.get("client_id", 0),
                  self.eng.tokens, self.eng.orders_in_event)
        self._say("fill", t_us=round(t, 3), purpose=purpose, qty=qty, price=od["price"], is_hedge=od["is_hedge"],
                  contract=od["contract"])

    # ------------------------------------------------------------ inputs
    def local_key(self, key):
        """Local switch input (stdin / CLI / tests). The ONLY way to arm or clear a latch."""
        self.local_in.put(key)

    def host_bytes(self, data):
        """Bytes from the host (TCP / file). Can never reach the switch."""
        self.frames_in.put(bytes(data))

    def _apply_local(self, key):
        e = self.eng
        if key == "a":
            if not self.arm_switch:                          # rising edge only
                self.arm_switch = True
                e._edge_now = True
            self._say("switch", t_us=self.now, sw9="high")
        elif key == "d":
            self.arm_switch = False
            e.armed = False
            self._say("switch", t_us=self.now, sw9="low")
        elif key == "c":                                     # KEY0: clear a latch, only with ARM low
            if self.arm_switch:
                self._say("key0", t_us=self.now, result="ignored: ARM is high")
                return
            try:
                e.local_rearm()
                self._latched_seen = 0
                self._say("key0", t_us=self.now, result="latch cleared (local)")
            except RuntimeError as ex:
                self._say("key0", t_us=self.now, result=f"refused: {ex}")

    def _apply_frame(self, ftype, payload):
        e, now = self.eng, self.now
        if ftype == fr.T_CONFIG:
            ok = self.cfg.apply(payload, e.armed)            # existing rules: range + disarmed only
            if ok:
                hb = e.p["hedge_band_shares"]
                e.p.update(self._engine_params(self.cfg.params, hb))
                e.thr = round(e.p["edge_threshold_ticks"] * 256)
            self._say("config", t_us=now, applied=ok, config_rejects=self.cfg.config_rejects)
        elif ftype == fr.T_KILL:
            if not e.kill:
                e.set_kill(now)
            self._say("kill", t_us=now, latched=e.latched)
        elif ftype == fr.T_CONTROL:
            op, arg = payload[0], payload[1]
            if op == 0x01:
                self.send_status()
            elif op == 0x02:
                self.status_period = arg * 10_000
                self.next_status = now + self.status_period if self.status_period else float("inf")
            elif op == 0x03:
                self.log_enabled = bool(arg & 1)
            else:
                self.frames_bad_extra += 1                   # FRAMES.md: unknown op counts in frames_bad
            self._say("control", t_us=now, op=op, arg=arg)
        elif ftype == fr.T_HEARTBEAT:
            hs = int.from_bytes(payload[:2], "big")
            if hs != self.host_hb["last_seq"]:
                self.host_hb["valid"] += 1
                self.host_hb["last_seq"] = hs
        elif ftype == fr.T_HEDGE_ORDER:
            cid, side = int.from_bytes(payload[0:2], "big"), payload[2] & 1
            qty, price = int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[5:8], "big") & 0x3FFFF
            od = dict(is_hedge=1, contract=0, side=side, qty=qty, price=price)
            if e.snap is None or e.und_q is None:
                self._log(1, now, 1, side, 0, qty, price, 0, F_BLOCKED | F_FROM_FRAME, 0, cid, e.tokens, e.orders_in_event)
            else:
                od_full = dict(od, client_id=cid)
                pre = g.gate_eval(e._gate_state(now, e.und_q), od)
                blocked = e.latched or not e.armed
                r = E.Engine.send(e, now, od, e.und_q, "host_hedge")
                self._on_decision(now, od_full, "host_hedge", r, pre, blocked)
        else:
            self.ignored[ftype] = self.ignored.get(ftype, 0) + 1   # TW6: validated, logged, not applied
            self._say("ignored", t_us=now, frame_type=f"0x{ftype:02X}", name=fr.FRAME_TYPES.get(ftype, "?"))

    def _drain_inputs(self, t):
        while self.scripted_local and self.scripted_local[0][0] <= t:
            self.local_in.put(self.scripted_local.pop(0)[1])
        while self.scripted_frames and self.scripted_frames[0][0] <= t:
            self.frames_in.put(self.scripted_frames.pop(0)[1])
        while not self.local_in.empty():
            self._apply_local(self.local_in.get())
        while not self.frames_in.empty():
            for ftype, payload in self.decoder.feed(self.frames_in.get()):
                self._apply_frame(ftype, payload)

    # ------------------------------------------------------------ main loop
    def _advance(self, t):
        """Status frames for every period boundary up to twin time t, then inputs at t."""
        while self.status_period and self.next_status <= t:
            if self.pace:
                self.pace(self.next_status)
            self.now = self.next_status
            self._drain_inputs(self.now)
            self.send_status()
            self.next_status += self.status_period
        if self.pace:
            self.pace(t)
        self.now = t
        self._drain_inputs(t)

    def _after(self):
        e = self.eng
        if e.snap is not None and e.snap is not self._snap_ref:
            self._snap_ref = e.snap
            self.snapshot_id += 1
        if len(e.latch_reasons) > self._latched_seen:
            self._latched_seen = len(e.latch_reasons)
            self._say("latch", t_us=self.now, reason=e.latch_reasons[-1]["reason"])
            self.send_status()                               # TW2: report the trip at once
        e._edge_now = False                                  # an edge counts for one step only

    def _messages(self):
        for m in self.sc.messages:
            self._advance(m["t"])
            t0 = time.perf_counter_ns()
            yield m
            self.twin_ns_last = time.perf_counter_ns() - t0  # twin_ns: wall time of this step (TW4)
            self.twin_ns_max = max(self.twin_ns_max, self.twin_ns_last)
            self._after()

    def run(self, idle_until=None):
        """Run the seeded session; returns the engine result (labeled software_twin)."""
        wrapped = _ScenarioView(self.sc, self._messages())
        res = self.eng.run(wrapped)
        self._after()
        end = self.now if idle_until is None else idle_until
        self._advance(max(end, self.now))
        self.send_status()                                   # final status
        res.update(source=SOURCE, timing=TIMING, ever_armed=self.ever_armed, seed=self.seed, regime=self.regime)
        return res


class _ScenarioView:
    """The scenario with its message list replaced by the twin's paced generator."""

    def __init__(self, sc, gen):
        self._sc, self.messages = sc, gen

    def __getattr__(self, k):
        return getattr(self._sc, k)


# ---------------------------------------------------------------- transports
def _stdin_reader(twin):
    for line in sys.stdin:
        for ch in line.strip().lower():
            if ch in "adc":
                twin.local_key(ch)


def serve(twin, port, speed, stdin=True, ready=None, idle_s=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    say(f"listening on 127.0.0.1:{srv.getsockname()[1]} (TCP loopback); waiting for one client", twin.out)
    if ready:
        ready(srv.getsockname()[1])
    conn, _ = srv.accept()
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    say("client connected; session starting", twin.out)
    twin.emit_bytes = lambda b: conn.sendall(b)

    def reader():
        while True:
            try:
                d = conn.recv(4096)
            except OSError:
                return
            if not d:
                return
            twin.host_bytes(d)
    threading.Thread(target=reader, daemon=True).start()
    if stdin:
        threading.Thread(target=_stdin_reader, args=(twin,), daemon=True).start()
    wall0 = time.monotonic()
    if speed > 0:
        twin.pace = lambda t_us: time.sleep(max(0.0, wall0 + t_us / 1e6 / speed - time.monotonic()))
    try:
        res = twin.run()
        say(json.dumps({"source": SOURCE, "timing": TIMING, "kind": "session_end",
                        **{k: res[k] for k in ("net_pnl", "fills", "hard_trips", "ever_armed")}}), twin.out)
        t_end = time.monotonic() + (idle_s if idle_s is not None else float("inf"))
        while time.monotonic() < t_end:                      # TW8: keep reporting status
            twin._advance(twin.now + twin.status_period if twin.status_period else twin.now + 100_000)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        say("client disconnected", twin.out)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)                  # graceful end of stream for the client
        except OSError:
            pass
        conn.close()
        srv.close()


def parse_switch(s):
    out = []
    for item in filter(None, (s or "").split(",")):
        key, t = item.split("@")
        if key not in ("a", "d", "c"):
            raise ValueError(f"switch key must be a, d or c: {item}")
        out.append((float(t), key))
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=BANNER)
    ap.add_argument("--seed", type=int, required=True, help="0-79 only")
    ap.add_argument("--regime", default="normal", choices=om.REGIMES)
    ap.add_argument("--hedge-band", type=int, required=True, help="200-1000 shares (no default, TW9)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--speed", type=float, default=0.01, help="twin time / wall time (TW8); 0 = no pacing")
    ap.add_argument("--out-file", help="write raw outbound frame bytes here instead of serving TCP")
    ap.add_argument("--in-file", help="file mode: raw inbound frame bytes, applied at t = 0")
    ap.add_argument("--switch", help="file mode: local switch script, e.g. a@60000,d@400000")
    a = ap.parse_args(argv)
    say(f"=== {BANNER} === SIMULATED arena; {TIMING}")
    try:
        check_seed(a.seed)
    except SeedRefused as ex:
        say(str(ex), sys.stderr)
        return 2
    if a.out_file:
        buf = bytearray()
        tw = Twin(a.seed, a.hedge_band, a.regime, emit=buf.extend)
        tw.scripted_local = parse_switch(a.switch)
        if a.in_file:
            tw.scripted_frames = [(0.0, open(a.in_file, "rb").read())]
        res = tw.run()
        with open(a.out_file, "wb") as f:
            f.write(buf)
        say(json.dumps({"source": SOURCE, "timing": TIMING, "kind": "file_written", "path": a.out_file,
                        "bytes": len(buf), **{k: res[k] for k in ("net_pnl", "fills", "hard_trips", "ever_armed",
                                                                  "determinism_hash")}}, sort_keys=True))
        return 0
    tw = Twin(a.seed, a.hedge_band, a.regime)
    say(f"arming: ONLY the local switch: type 'a' + Enter = {ARM_SWITCH}; 'd' = SW9 low; 'c' = KEY0 clear (ARM low)")
    serve(tw, a.port, a.speed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
