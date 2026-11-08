// tb_gate.sv - self-checking Questa testbench for fpga/gate/gate.sv.
//
// Part A  gate_core golden replay: every row of docs/vectors/gate.csv (via gate_vectors.mem,
//         made by gate_csv2mem.py) is driven one per cycle; reason, all 15 fail_mask bits and
//         reduce_only are compared with golden gate_eval(). Run twice: back-to-back and with
//         random pipeline bubbles. Then a coverage gate: every check R01..R15 must have been
//         seen tripping and not tripping, every non-boolean check must have its boundary pair,
//         and every exemption/non-exemption kind must be present. Missing coverage = FAIL.
// Part B  gate_core directed: R04 jump limit at the current calibration (370 c = 5920 raw),
//         including the old 185 c and 200 c values, which must now pass.
// Part C  gate (top) directed: latency and order_ready, armed/tripped blocking, token bucket
//         (burst, refill, saturation, refill+consume net-out), orders-in-event and event_start,
//         latch_req bits, R13 latch persistence and its clear path.
//
// Prints a PASS/FAIL count and "TB PASS" / "TB FAIL". run_tb_gate.ps1 turns that into the
// process exit code (nonzero on any failure).
`timescale 1ns/1ps

module tb_gate;
    `include "gate_vec_pkg.svh"

    localparam logic [14:0] SKIP = 15'b011_0111_1010_1111;   // reduce-only exempt checks (golden)
    localparam int    JUMP_RAW   = 370 * 16;                 // golden.py JUMP_LIMIT_CENTS * 16
    localparam longint EQ_LIM    = -64'sd12_800_000;         // R13: -2000 * 6400
    localparam int    REFILL     = 5000;

    logic clk = 0;
    always #10 clk = ~clk;                                   // 50 MHz

    int n_pass = 0, n_fail = 0;
    task automatic check(bit cond, string msg);
        if (cond) n_pass++;
        else begin n_fail++; $display("FAIL  t=%0t  %s", $time, msg); end
    endtask

    // ------------------------------------------------------------ vectors
    logic [VEC_W-1:0] raw [N_VEC];
    gate_vec_t vecs [N_VEC];
    string mem_file;

    // ============================================================ gate_core instance (A, B)
    logic      c_rst_n = 0, c_in_valid = 0;
    gate_vec_t cv;
    logic      c_out_valid, c_reduce;
    logic [3:0]  c_reason;
    logic [14:0] c_mask;

    gate_core u_core (
        .clk, .rst_n(c_rst_n), .in_valid(c_in_valid),
        .is_hedge(cv.is_hedge), .side(cv.side), .qty(cv.qty), .price(cv.price),
        .now_us(cv.now_us), .snap_flip_us(cv.snap_flip_us), .ref_seq(cv.ref_seq),
        .feed_seq(cv.feed_seq), .feed_bad(cv.feed_bad), .S(cv.S), .S_ref(cv.S_ref),
        .q_bid(cv.q_bid), .q_ask(cv.q_ask), .q_bsz(cv.q_bsz), .q_asz(cv.q_asz),
        .mp_q4(cv.mp_q4), .theo_0_c(cv.theo_0_c), .delta_adj_c(cv.delta_adj_c),
        .gamma_c_c(cv.gamma_c_c), .vega_c(cv.vega_c), .pos_c(cv.pos_c), .sh(cv.sh),
        .sum_delta(cv.sum_delta), .sum_gamma(cv.sum_gamma), .sum_vega(cv.sum_vega),
        .equity_x6400(cv.equity_x6400), .hb_last_us(cv.hb_last_us), .kill(cv.kill),
        .tokens(cv.tokens), .orders_in_event(cv.orders_in_event), .r13_force(1'b0),
        .out_valid(c_out_valid), .reason(c_reason), .fail_mask(c_mask), .reduce_only(c_reduce));

    // ============================================================ gate top instance (C)
    logic      t_rst_n = 0, tick_1us = 0, event_start = 0, armed = 1, tripped = 0, kill_frame = 0;
    logic      order_valid = 0;
    gate_vec_t tv;
    logic        order_ready, result_valid, approve, t_reduce, blocked, reject_pulse, r13_latched;
    logic [3:0]  reason_code, reject_code, orders_in_event;
    logic [14:0] t_mask;
    logic [4:0]  latch_req, tokens;
    logic [2:0]  result_contract;
    logic        result_is_hedge;

    gate u_gate (
        .clk, .rst_n(t_rst_n), .tick_1us, .now_us(tv.now_us), .event_start, .armed, .tripped,
        .kill_frame, .order_valid, .order_ready,
        .is_hedge(tv.is_hedge), .contract(tv.contract), .side(tv.side), .qty(tv.qty), .price(tv.price),
        .snap_flip_us(tv.snap_flip_us), .ref_seq(tv.ref_seq), .feed_seq(tv.feed_seq),
        .feed_bad(tv.feed_bad), .S(tv.S), .S_ref(tv.S_ref),
        .q_bid(tv.q_bid), .q_ask(tv.q_ask), .q_bsz(tv.q_bsz), .q_asz(tv.q_asz),
        .mp_q4(tv.mp_q4), .theo_0_c(tv.theo_0_c), .delta_adj_c(tv.delta_adj_c),
        .gamma_c_c(tv.gamma_c_c), .vega_c(tv.vega_c), .pos_c(tv.pos_c), .sh(tv.sh),
        .sum_delta(tv.sum_delta), .sum_gamma(tv.sum_gamma), .sum_vega(tv.sum_vega),
        .equity_x6400(tv.equity_x6400), .hb_last_us(tv.hb_last_us), .kill(tv.kill),
        .result_valid, .approve, .reason_code, .fail_mask(t_mask), .reduce_only(t_reduce),
        .blocked, .latch_req, .reject_pulse, .reject_code, .tokens, .orders_in_event,
        .r13_latched, .result_contract, .result_is_hedge);

    // 1 us tick: 0 = off, 1 = real (every 50 clocks), 2 = every clock (fast refill tests)
    int tick_mode = 0, tick_div = 0;
    longint n_ticks = 0;
    always @(posedge clk) begin
        tick_div <= (tick_div == 49) ? 0 : tick_div + 1;
        case (tick_mode)
            1:       tick_1us <= (tick_div == 49);
            2:       tick_1us <= 1'b1;
            default: tick_1us <= 1'b0;
        endcase
        if (tick_1us) n_ticks <= n_ticks + 1;
    end

    // latch_req[3] (R14) is tied to 0 by the gate; check it on every clock of part C
    bit watch_lr3 = 0;
    int lr3_bad = 0;
    always @(negedge clk) if (watch_lr3 && latch_req[3] !== 1'b0) lr3_bad++;

    // ============================================================ part A: golden replay
    // per-check coverage (counted only on rows that matched)
    int cov_trip[16], cov_clear[16], cov_bnd_pass[16], cov_bnd_fail[16];
    int cov_exempt[16], cov_nonred[16], cov_applies[16], cov_pair[16], cov_cross, cov_tozero;
    int a_vec_fail = 0, a_shown = 0;

    function automatic string row_str(int i, gate_vec_t v);
        return $sformatf("vector %0d (gate.csv line %0d) kind=%h target=%0d target2=%0d hedge=%0d",
                         i, i + 2, v.kind, v.target, v.target2, v.is_hedge);
    endfunction

    task automatic compare_core(int i, string pass_name);
        gate_vec_t v = vecs[i];
        bit ok = (c_reason === v.exp_reason) && (c_mask === v.exp_fail_mask)
                 && (c_reduce === v.exp_reduce_only);
        if (ok) n_pass++;
        else begin
            n_fail++; a_vec_fail++;
            if (a_shown++ < 60)
                $display("MISMATCH [%s] %s: expected reason=%0d fail_mask=%04h reduce=%0d, got reason=%0d fail_mask=%04h reduce=%0d",
                         pass_name, row_str(i, v), v.exp_reason, v.exp_fail_mask, v.exp_reduce_only,
                         c_reason, c_mask, c_reduce);
            return;
        end
        if (pass_name != "back-to-back") return;            // count coverage once
        for (int k = 1; k <= 15; k++) begin
            if (v.exp_fail_mask[k-1]) cov_trip[k]++; else cov_clear[k]++;
        end
        if (v.target != 0) case (v.kind)
            4'h2: cov_bnd_pass[v.target]++;
            4'h3: cov_bnd_fail[v.target]++;
            4'h4: cov_pair[v.target]++;
            4'h5: cov_exempt[v.target]++;
            4'h6: cov_nonred[v.target]++;
            4'h7: cov_applies[v.target]++;
            4'h8: cov_cross++;
            4'h9: cov_tozero++;
            default: ;
        endcase
    endtask

    task automatic replay(string pass_name, int bubble_pct);
        int q[$];
        int got = 0, cycles = 0;
        c_in_valid = 0;
        fork
            begin
                for (int i = 0; i < N_VEC; i++) begin
                    @(negedge clk);
                    while (bubble_pct > 0 && ($urandom_range(99) < bubble_pct)) begin
                        c_in_valid = 0; cv = vecs[$urandom_range(N_VEC - 1)];   // garbage on bubbles
                        @(negedge clk);
                    end
                    cv = vecs[i]; c_in_valid = 1; q.push_back(i);
                end
                @(negedge clk) c_in_valid = 0;
            end
            begin
                while (got < N_VEC && cycles < 20 * N_VEC + 100) begin
                    @(negedge clk); cycles++;
                    if (c_out_valid) begin
                        if (q.size() == 0) begin check(0, {pass_name, ": out_valid with no vector in flight"}); end
                        else begin compare_core(q.pop_front(), pass_name); got++; end
                    end
                end
            end
        join
        check(got == N_VEC, $sformatf("%s: %0d of %0d results came out", pass_name, got, N_VEC));
        repeat (5) @(negedge clk);
        check(c_out_valid == 0, {pass_name, ": out_valid idle after the last vector"});
    endtask

    task automatic coverage_gate();
        int miss = 0;
        $display("  check | trip  clear | bnd@lim bnd+1 | pair | exempt nonred applies");
        for (int k = 1; k <= 15; k++) begin
            bit boolean = (k == 3 || k == 15);
            bit skip    = SKIP[k-1];
            $display("  R%02d   | %4d  %5d | %5d  %5d  | %3d  | %4d  %5d  %5d", k, cov_trip[k], cov_clear[k],
                     cov_bnd_pass[k], cov_bnd_fail[k], cov_pair[k], cov_exempt[k], cov_nonred[k], cov_applies[k]);
            if (cov_trip[k] == 0 || cov_clear[k] == 0)                  begin miss++; $display("    R%02d: no trip or no clear row", k); end
            if (!boolean && (cov_bnd_pass[k] == 0 || cov_bnd_fail[k] == 0)) begin miss++; $display("    R%02d: boundary pair missing", k); end
            if (skip && (cov_exempt[k] == 0 || cov_nonred[k] == 0))     begin miss++; $display("    R%02d: exemption pair missing", k); end
            if (!skip && cov_applies[k] == 0)                           begin miss++; $display("    R%02d: reduce-only-still-applies row missing", k); end
        end
        $display("  cross-through-zero rows: %0d, reduce-to-zero rows: %0d", cov_cross, cov_tozero);
        if (cov_cross == 0 || cov_tozero == 0) miss++;
        check(miss == 0, $sformatf("golden coverage gate: %0d holes", miss));
    endtask

    // ============================================================ part B helpers
    task automatic core_one(gate_vec_t v, logic [14:0] exp_mask, logic exp_red, string msg);
        logic [3:0] exp_reason = 0;
        for (int k = 14; k >= 0; k--) if (exp_mask[k]) exp_reason = k + 1;
        @(negedge clk); cv = v; c_in_valid = 1;
        @(negedge clk); c_in_valid = 0;
        repeat (2) @(negedge clk);
        check(c_out_valid === 1 && c_mask === exp_mask && c_reason === exp_reason && c_reduce === exp_red,
              $sformatf("%s: expected mask=%04h reason=%0d reduce=%0d, got valid=%0d mask=%04h reason=%0d reduce=%0d",
                        msg, exp_mask, exp_reason, exp_red, c_out_valid, c_mask, c_reason, c_reduce));
    endtask

    // ============================================================ part C helpers
    typedef struct {
        int          latency;     // negedges from order_valid to result_valid
        logic        approve, reduce, blocked, reject;
        logic [3:0]  reason;
        logic [14:0] mask;
        logic [4:0]  latch_req;
    } res_t;

    task automatic top_reset();
        @(negedge clk); t_rst_n = 0; order_valid = 0; event_start = 0; kill_frame = 0;
        repeat (3) @(negedge clk); t_rst_n = 1;
        @(negedge clk);
    endtask

    // Send the order in tv. If ev_at_result, pulse event_start in the result cycle (G10).
    task automatic send(output res_t r, input bit ev_at_result = 0);
        int n = 0;
        @(negedge clk);
        while (!order_ready) @(negedge clk);
        order_valid = 1;
        @(negedge clk); order_valid = 0; n = 1;
        while (!result_valid && n < 20) begin @(negedge clk); n++; end
        r.latency = n; r.approve = approve; r.reduce = t_reduce; r.blocked = blocked;
        r.reject = reject_pulse; r.reason = reason_code; r.mask = t_mask; r.latch_req = latch_req;
        if (ev_at_result) event_start = 1;
        @(negedge clk); event_start = 0;
    endtask

    task automatic pulse_event();
        @(negedge clk) event_start = 1;
        @(negedge clk) event_start = 0;
    endtask

    // wait until tokens changes (or limit ticks), return the ticks elapsed
    task automatic ticks_until_tokens(input int want, input int limit, output longint dt);
        longint t0 = n_ticks;
        while (tokens != want && (n_ticks - t0) <= limit) @(negedge clk);
        dt = n_ticks - t0;
    endtask

    // ============================================================ main
    initial begin
        res_t r;
        gate_vec_t base, v;
        longint dt;

        if (!$value$plusargs("MEM=%s", mem_file)) mem_file = "gate_vectors.mem";
        $readmemh(mem_file, raw);
        for (int i = 0; i < N_VEC; i++) vecs[i] = gate_vec_t'(raw[i]);
        if ($isunknown(raw[N_VEC - 1])) begin
            $display("FAIL  could not read %0d vectors from %s", N_VEC, mem_file);
            $display("tb_gate: 0 passed, 1 failed"); $display("TB FAIL"); $finish;
        end
        base = vecs[0];
        if (!(base.kind == 0 && base.exp_reason == 0 && base.is_hedge == 0))
            $display("WARN  vector 0 is not an option baseline; part B/C base state may be wrong");

        // ---------------------------------------------------- part A
        $display("== A: gate_core golden replay, %0d vectors from %s", N_VEC, mem_file);
        cv = base;
        repeat (3) @(negedge clk); c_rst_n = 1;
        replay("back-to-back", 0);
        void'($urandom(20261108));
        replay("bubbles", 30);
        $display("   golden replay: %0d vector mismatches (of %0d per pass x 2 passes)", a_vec_fail, N_VEC);
        coverage_gate();

        // ---------------------------------------------------- part B: R04 at 370 c
        $display("== B: R04 jump limit = %0d raw (370 c)", JUMP_RAW);
        begin
            automatic int dl [] = '{0, 185*16, 185*16 + 1, 200*16, 200*16 + 1, JUMP_RAW - 1, JUMP_RAW, JUMP_RAW + 1,
                          -JUMP_RAW, -(JUMP_RAW + 1), 1023*16};
            foreach (dl[i]) begin
                v = base; v.S = 21'(int'(base.S_ref) + dl[i]);
                core_one(v, (dl[i] > JUMP_RAW || dl[i] < -JUMP_RAW) ? 15'h0008 : 15'h0000, 1'b0,
                         $sformatf("R04 option, S - S_ref = %0d raw (%0.4f c)", dl[i], dl[i] / 16.0));
            end
            // reduce-only exempts R04 (pos +3, sell 1)
            v = base; v.S = 21'(int'(base.S_ref) + JUMP_RAW + 1); v.pos_c = 9'sd3; v.side = 1;
            core_one(v, 15'h0000, 1'b1, "R04 +5921 raw, reduce-only sell: exempt");
            // hedge order also checks R04
            v = vecs[16]; v.S = 21'(int'(v.S_ref) - (JUMP_RAW + 1));
            if (v.is_hedge && v.kind == 0) core_one(v, 15'h0008, 1'b0, "R04 hedge, -5921 raw");
            else check(0, "vector 16 is not the hedge baseline (part B hedge R04 not run)");
        end

        // ---------------------------------------------------- part C: gate top
        $display("== C: gate top");
        tv = base; tv.equity_x6400 = 0; tv.feed_bad = 0; tv.kill = 0;
        armed = 1; tripped = 0; tick_mode = 1; watch_lr3 = 1;
        top_reset();
        check(tokens == 20 && orders_in_event == 0 && r13_latched == 0 && order_ready == 1 && latch_req == 0,
              $sformatf("C0 after reset: tokens=%0d oie=%0d r13=%0d ready=%0d latch_req=%b",
                        tokens, orders_in_event, r13_latched, order_ready, latch_req));

        // C1 latency, order_ready, result identity, approval accounting
        tv.contract = 3'd0;
        // accept at clock P0; result_valid in the cycle after P2 (3 cycles); order_ready returns
        // after P4 (one settle cycle after result_valid, GATE_SPEC section 6, SETTLE_CYCLES = 1)
        begin
            automatic int lo = 0;
            @(negedge clk); order_valid = 1;
            @(negedge clk); order_valid = 0;
            for (int n = 1; n <= 7; n++) begin
                if (n <= 4) lo += !order_ready;
                if (n == 3) check(result_valid && approve && reason_code == 0 && !blocked && !reject_pulse,
                                  "C1 result_valid + approve exactly 3 cycles after accept");
                if (n != 3) check(!result_valid, $sformatf("C1 no result_valid at cycle %0d", n));
                if (n == 5) check(order_ready, "C1 order_ready back after the settle cycle");
                @(negedge clk);
            end
            check(lo == 4, $sformatf("C1 order_ready low for the 4 busy cycles (%0d)", lo));
            check(tokens == 19 && orders_in_event == 1, $sformatf("C1 approve consumed a token and counted: tokens=%0d oie=%0d", tokens, orders_in_event));
            check(result_contract == 0 && result_is_hedge == 0, "C1 result identity");
        end

        // C2 blocked: armed low, then tripped high
        armed = 0; send(r);
        check(!r.approve && r.blocked && r.reject && r.reason == 0, "C2 armed=0: checks pass but approve=0, blocked=1, reject_pulse=1");
        armed = 1; tripped = 1; send(r);
        check(!r.approve && r.blocked && r.reject && r.reason == 0, "C2 tripped=1: approve=0, blocked=1");
        tripped = 0;
        check(tokens == 19 && orders_in_event == 1, "C2 blocked orders consume no token and do not count");
        v = tv; tv.S = 21'(int'(tv.S_ref) + JUMP_RAW + 1); send(r); tv = v;
        check(!r.approve && r.reason == 4 && tokens == 19, "C2 R04 reject consumes no token");

        // C3 orders in event: 8 pass, the 9th fails R12; event_start clears
        pulse_event();
        check(orders_in_event == 0, "C3 event_start clears orders_in_event");
        for (int i = 1; i <= 8; i++) begin
            send(r);
            check(r.approve && r.reason == 0 && !r.latch_req[1], $sformatf("C3 order %0d of event approved", i));
        end
        send(r);
        check(!r.approve && r.reason == 12 && r.mask[11] && r.latch_req[1] && orders_in_event == 8,
              $sformatf("C3 9th order in event fails R12 + latch_req[1]: reason=%0d lr=%b oie=%0d", r.reason, r.latch_req, orders_in_event));
        @(negedge clk);
        check(latch_req[1] == 0, "C3 latch_req[1] is a pulse (low once result_valid drops)");
        // G11: R12 also failing while a lower code is reported still raises latch_req[1]
        v = tv; tv.S = 21'(int'(tv.S_ref) + JUMP_RAW + 1); send(r); tv = v;
        check(r.reason == 4 && r.mask[11] && r.latch_req[1], "C3 G11: reason R04 with R12 also failing raises latch_req[1]");
        // G10: approval in the same cycle as event_start counts toward the new event
        pulse_event();
        send(r);                                                // oie = 1
        send(r, 1);                                             // approve + event_start together
        check(r.approve && orders_in_event == 1, $sformatf("C3 G10 approve with event_start -> oie=1, not 0 or 2 (got %0d)", orders_in_event));

        // C4 token bucket: 20 orders inside 1 ms all pass, the 21st fails R12 (tokens == 0)
        top_reset();
        begin
            automatic longint t0 = n_ticks;
            automatic int ok = 0;
            for (int i = 1; i <= 20; i++) begin
                pulse_event(); send(r);
                ok += (r.approve && r.reason == 0);
            end
            pulse_event(); send(r);
            check(ok == 20, $sformatf("C4 20 orders in %0d us all approved (%0d)", n_ticks - t0, ok));
            check(n_ticks - t0 < 1000, "C4 burst fits inside 1 ms");
            check(!r.approve && r.reason == 12 && r.latch_req[1] && tokens == 0, $sformatf("C4 21st fails R12, tokens=%0d", tokens));
        end
        // refill: 1 token per 5,000 ticks, saturating at 20
        tick_mode = 2;
        ticks_until_tokens(1, REFILL + 10, dt);
        check(tokens == 1 && dt <= REFILL, $sformatf("C4 first refill within 5000 ticks (%0d)", dt));
        ticks_until_tokens(2, REFILL + 10, dt);
        check(tokens == 2 && dt == REFILL, $sformatf("C4 next refill exactly 5000 ticks later (%0d)", dt));
        // refill and consume in the same cycle net out (G9): approve lands on the refill clock
        // valid set when refill_cnt == 4996 -> accept at P0, result at P2..P3, refill at P3
        begin
            automatic int tok0;
            pulse_event();
            while (u_gate.refill_cnt != REFILL - 4) @(negedge clk);
            tok0 = tokens;
            order_valid = 1; @(negedge clk); order_valid = 0;
            @(negedge clk); @(negedge clk);
            check(result_valid && approve && u_gate.refill, "C4 G9 setup: approve in the refill cycle");
            @(negedge clk);
            check(tokens == tok0, $sformatf("C4 G9 refill + consume net out: %0d -> %0d", tok0, tokens));
        end
        ticks_until_tokens(20, 25 * REFILL, dt);
        check(tokens == 20, $sformatf("C4 refills up to 20 (%0d)", tokens));
        repeat (2 * REFILL + 10) @(negedge clk);
        check(tokens == 20, $sformatf("C4 saturates at 20 (%0d)", tokens));
        tick_mode = 1;

        // C5 R13 latch: set, persistence after recovery, clear only via arming clear (tripped falling)
        top_reset(); pulse_event();
        tv.equity_x6400 = EQ_LIM;                               // exactly at the limit: not a trip
        repeat (3) @(negedge clk);
        check(!r13_latched && !latch_req[2], "C5 equity == -12,800,000 does not set R13");
        send(r);
        check(r.approve && r.reason == 0, "C5 equity at the limit: order approved");
        tv.equity_x6400 = EQ_LIM - 1;                           // one unit beyond, no order in flight
        #1 check(latch_req[2] == 1, "C5 latch_req[2] raised combinationally by the equity condition");
        @(negedge clk);
        check(r13_latched, "C5 equity -12,800,001 latches R13 with no order in flight");
        tv.equity_x6400 = 0;                                    // input recovers
        repeat (1000) @(negedge clk);
        check(r13_latched && latch_req[2], "C5 R13 latch and latch_req[2] persist after equity recovers");
        send(r);
        check(!r.approve && r.reason == 13 && r.mask[12], $sformatf("C5 recovered equity, latched: non-reduce order fails R13 (reason=%0d)", r.reason));
        v = tv; tv.pos_c = 9'sd5; tv.side = 1;                  // reduce-only sell 1 of +5
        send(r);
        check(r.reduce && r.approve && r.reason == 0, "C5 latched: reduce-only order is exempt from R13");
        tripped = 1; send(r);
        check(r.reduce && !r.approve && r.blocked && r.reason == 0, "C5 G7: reduce-only blocked while tripped");
        tv = v;
        // things that must NOT clear it
        armed = 0; repeat (3) @(negedge clk); armed = 1; repeat (3) @(negedge clk);
        check(r13_latched, "C5 armed toggle does not clear R13");
        pulse_event(); kill_frame = 1; @(negedge clk); kill_frame = 0; repeat (3) @(negedge clk);
        check(r13_latched, "C5 event_start / kill_frame do not clear R13");
        repeat (100) @(negedge clk);
        check(r13_latched, "C5 tripped held high does not clear R13");
        tv.equity_x6400 = EQ_LIM - 1; @(negedge clk);
        tripped = 0;                                            // arming clears while equity still bad
        repeat (3) @(negedge clk);
        check(r13_latched, "C5 tripped falling while equity is still bad: stays latched");
        tripped = 1; repeat (3) @(negedge clk);
        tv.equity_x6400 = 0; repeat (3) @(negedge clk);
        check(r13_latched, "C5 still latched before the clear");
        tripped = 0;                                            // the allowed path
        @(negedge clk);
        check(!r13_latched, "C5 tripped falling with equity ok clears R13 (one clock)");
        check(latch_req[2] == 0, "C5 latch_req[2] low after the clear");
        send(r);
        check(r.approve && r.reason == 0, "C5 after the clear, orders are approved again");
        tv.equity_x6400 = EQ_LIM - 1; @(negedge clk); tv.equity_x6400 = 0; @(negedge clk);
        check(r13_latched, "C5 re-latches on a one-cycle equity excursion");
        @(negedge clk); t_rst_n = 0; @(negedge clk); t_rst_n = 1; @(negedge clk);
        check(!r13_latched, "C5 reset clears R13");

        // C6 latch_req[0] = feed_bad level (no order needed), latch_req[4] = kill_frame
        tv.feed_bad = 1; #1 check(latch_req[0] == 1, "C6 latch_req[0] follows feed_bad with no order");
        @(negedge clk); tv.feed_bad = 0; #1 check(latch_req[0] == 0, "C6 latch_req[0] drops with feed_bad (arming holds it)");
        kill_frame = 1; #1 check(latch_req[4] == 1, "C6 latch_req[4] follows kill_frame");
        @(negedge clk); kill_frame = 0; #1 check(latch_req[4] == 0, "C6 latch_req[4] is a pulse");
        tv.kill = 1; send(r); tv.kill = 0;
        check(!r.approve && r.reason == 15, "C6 kill input -> R15 reject");
        watch_lr3 = 0;
        check(lr3_bad == 0, $sformatf("C6 latch_req[3] (R14) stayed 0 for all of part C (%0d bad cycles)", lr3_bad));

        // ---------------------------------------------------- summary
        $display("tb_gate: %0d passed, %0d failed", n_pass, n_fail);
        if (n_fail == 0) $display("TB PASS"); else $display("TB FAIL");
        $finish;
    end

    initial begin
        #200ms;
        $display("FAIL  watchdog timeout");
        $display("tb_gate: %0d passed, %0d failed", n_pass, n_fail + 1);
        $display("TB FAIL");
        $finish;
    end
endmodule
