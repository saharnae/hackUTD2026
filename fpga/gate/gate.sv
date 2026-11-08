// gate.sv - hardware risk gate: 15 checks, reduce-only exemptions, R12 rate state, R13 latch.
// Target: Quartus Prime Lite, 10M50DAF484C7G, one 50 MHz clock. No latches, no initial blocks.
// Reference: golden/golden.py gate_eval() (A16-A21); spec: fpga/gate/GATE_SPEC.md.
//
// gate_core : stateless 3-stage pipeline, every input a port; bit-exact to gate_eval()
//             (replayed against docs/vectors/gate.csv by tb_gate.sv).
// gate      : wraps gate_core; owns the token bucket, the orders-in-event counter, the R13
//             latch, one-order-in-flight control and the latch requests to arming.
//
// ASSUMPTIONS (G1-G6 in GATE_SPEC.md, plus):
//  G7.  approve also needs armed & ~tripped, sampled when the result appears. A trip during
//       an order in flight therefore blocks it. Reduce-only orders are blocked too while a
//       trip is latched (the exemptions only matter while not tripped).
//  G8.  R13 latch: set while equity_x6400 < -12,800,000; cleared only when arming clears
//       (tripped falls: KEY0 with ARM low). While set, it forces check 13 in the core, so
//       non-reduce orders report R13 even after equity recovers.
//  G9.  Token refill: a free-running count of 5,000 tick_1us adds 1 token, saturating at 20.
//       Refill and consume in the same cycle net out.
//  G10. An approval in the same cycle as event_start counts toward the new event.
//  G11. latch_req[1] (R12) is raised by any result with fail_mask bit 11 set, even if a
//       lower code is the reported reason. latch_req[0] (R03) follows feed_bad (level).

module gate_core #(
    parameter int unsigned STALE_US       = 250_000,      // R01
    parameter int unsigned SYNC_MAX_BEHIND = 64,          // R02
    parameter int unsigned JUMP_LIMIT_RAW = 370 * 16,     // R04: jump_limit $3.70, cents Q4. Source: golden.py JUMP_LIMIT_CENTS (error_curve.py adopted_jump_limit, SPY). TODO(FINAL)
    parameter int unsigned HEARTBEAT_US   = 150_000       // R14
) (
    input  logic                clk,
    input  logic                rst_n,
    input  logic                in_valid,

    input  logic                is_hedge,
    input  logic                side,               // 0 buy, 1 sell
    input  logic        [15:0]  qty,
    input  logic        [17:0]  price,

    input  logic        [31:0]  now_us,
    input  logic        [31:0]  snap_flip_us,
    input  logic        [15:0]  ref_seq,
    input  logic        [15:0]  feed_seq,
    input  logic                feed_bad,
    input  logic        [20:0]  S,
    input  logic        [20:0]  S_ref,
    input  logic        [17:0]  q_bid,
    input  logic        [17:0]  q_ask,
    input  logic        [11:0]  q_bsz,
    input  logic        [11:0]  q_asz,
    input  logic        [23:0]  mp_q4,
    input  logic        [17:0]  theo_0_c,
    input  logic signed [17:0]  delta_adj_c,
    input  logic        [17:0]  gamma_c_c,
    input  logic        [15:0]  vega_c,
    input  logic signed [8:0]   pos_c,              // conf + pend of the ordered contract
    input  logic signed [16:0]  sh,                 // hedge shares conf + pend
    input  logic signed [31:0]  sum_delta,
    input  logic signed [31:0]  sum_gamma,
    input  logic signed [31:0]  sum_vega,
    input  logic signed [47:0]  equity_x6400,
    input  logic        [31:0]  hb_last_us,
    input  logic                kill,
    input  logic        [4:0]   tokens,
    input  logic        [3:0]   orders_in_event,
    input  logic                r13_force,          // gate's R13 latch (0 for golden replay)

    output logic                out_valid,
    output logic        [3:0]   reason,
    output logic        [14:0]  fail_mask,
    output logic                reduce_only
);
    // bit k-1 = check k exempt for reduce-only: R01-R04, R06, R08-R11, R13, R14
    localparam logic [14:0] SKIP = 15'b011_0111_1010_1111;

    // ------------------------------------------------------------ stage 1 (input regs)
    wire signed [16:0] sq_w = side ? -$signed({1'b0, qty}) : $signed({1'b0, qty});
    wire signed [16:0] oq_w = is_hedge ? 17'sd0 : sq_w;

    logic               v1, h1;
    logic        [15:0] qty1;
    logic signed [16:0] sq1;
    logic signed [34:0] pd1;            // oq * delta_adj
    logic signed [35:0] pg1;            // oq * gamma_c
    logic signed [33:0] pv1;            // oq * vega
    logic        [31:0] dt_snap1, dt_hb1;
    logic        [15:0] dseq1;
    logic signed [21:0] ds1;
    logic signed [26:0] band_o1;
    logic signed [24:0] band_h1;
    logic signed [17:0] pos_after1;
    logic signed [16:0] p1;             // position being reduced
    logic signed [17:0] sh_after1;
    logic signed [31:0] sd1, sg1, sv1;
    logic               r03_1, r05_1, r12_1, r13_1, r15_1;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            v1 <= 1'b0;
        end else begin
            v1 <= in_valid;
        end
        h1         <= is_hedge;
        qty1       <= qty;
        sq1        <= sq_w;
        pd1        <= oq_w * delta_adj_c;
        pg1        <= oq_w * $signed({1'b0, gamma_c_c});
        pv1        <= oq_w * $signed({1'b0, vega_c});
        dt_snap1   <= now_us - snap_flip_us;
        dt_hb1     <= now_us - hb_last_us;
        dseq1      <= feed_seq - ref_seq;
        ds1        <= $signed({1'b0, S}) - $signed({1'b0, S_ref});
        band_o1    <= $signed({1'b0, price, 8'd0}) - $signed({9'd0, theo_0_c});
        band_h1    <= $signed({3'd0, price, 4'd0}) - $signed({1'b0, mp_q4});
        pos_after1 <= pos_c + sq_w;
        p1         <= is_hedge ? sh : 17'(pos_c);
        sh_after1  <= sh + (is_hedge ? sq_w : 17'sd0);
        sd1        <= sum_delta;
        sg1        <= sum_gamma;
        sv1        <= sum_vega;
        r03_1      <= feed_bad;
        r05_1      <= !(q_bid != 18'd0 && q_ask > q_bid && (q_ask - q_bid) <= 18'd20
                        && q_bsz != 12'd0 && q_asz != 12'd0);
        r12_1      <= (orders_in_event >= 4'd8) || (tokens == 5'd0);
        r13_1      <= (equity_x6400 < -48'sd12_800_000) || r13_force;
        r15_1      <= kill;
    end

    // ------------------------------------------------------------ stage 2 (all compares)
    wire signed [35:0] d_sum  = sd1 + pd1;
    wire signed [43:0] net_d  = 44'(d_sum) * 44'sd100 + (44'(sh_after1) <<< 16);
    wire signed [36:0] g_sum  = sg1 + pg1;
    wire signed [43:0] net_g  = 44'(g_sum) * 44'sd100;
    wire signed [34:0] net_v  = sv1 + pv1;

    function automatic logic abs_gt(input logic signed [43:0] x, input logic signed [43:0] lim);
        return (x > lim) || (x < -lim);
    endfunction

    wire [14:0] fail_w = {
        r15_1,                                                          // R15
        dt_hb1 > HEARTBEAT_US,                                          // R14
        r13_1,                                                          // R13
        r12_1,                                                          // R12
        abs_gt(44'(net_v), 44'sd384_000),                               // R11: golden.py NET_VEGA_LIMIT 1500 * 256 ($1,500/vol pt, SPY). TODO(FINAL)
        abs_gt(net_g, 44'sd25_165_824),                                 // R10
        abs_gt(net_d, 44'sd131_072_000),                                // R09
        !h1 && abs_gt(44'(pos_after1), 44'sd20),                        // R08
        qty1 > (h1 ? 16'd1000 : 16'd10),                                // R07
        h1 ? abs_gt(44'(band_h1), 44'sd160) : abs_gt(44'(band_o1), 44'sd2560),   // R06
        r05_1,                                                          // R05
        abs_gt(44'(ds1), 44'(JUMP_LIMIT_RAW)),                          // R04
        r03_1,                                                          // R03
        dseq1 > 16'(SYNC_MAX_BEHIND),                                   // R02
        dt_snap1 > STALE_US                                             // R01
    };
    wire signed [16:0] abs_p = p1[16] ? -p1 : p1;
    wire reduce_w = (p1 != 17'sd0) && (sq1 != 17'sd0) && (p1[16] != sq1[16])
                    && ($signed({1'b0, qty1}) <= abs_p);

    logic        v2, red2;
    logic [14:0] fail2;
    always_ff @(posedge clk) begin
        if (!rst_n) v2 <= 1'b0;
        else        v2 <= v1;
        fail2 <= fail_w;
        red2  <= reduce_w;
    end

    // ------------------------------------------------------------ stage 3 (mask, priority)
    wire [14:0] mask_w = fail2 & ~(red2 ? SKIP : 15'd0);
    logic [3:0] code_w;
    always_comb begin
        code_w = 4'd0;
        for (int k = 14; k >= 0; k--)
            if (mask_w[k]) code_w = 4'(k + 1);
    end

    always_ff @(posedge clk) begin
        if (!rst_n) out_valid <= 1'b0;
        else        out_valid <= v2;
        fail_mask   <= mask_w;
        reason      <= code_w;
        reduce_only <= red2;
    end
endmodule


module gate #(
    parameter int unsigned TOKENS_MAX       = 20,
    parameter int unsigned REFILL_US        = 5_000,
    parameter int unsigned MAX_PER_EVENT    = 8,     // documented; the core compares >= 8
    parameter int unsigned SETTLE_CYCLES    = 1      // position keeper update after a result
) (
    input  logic                clk,
    input  logic                rst_n,              // synchronous, active low
    input  logic                tick_1us,
    input  logic        [31:0]  now_us,
    input  logic                event_start,
    input  logic                armed,
    input  logic                tripped,
    input  logic                kill_frame,         // one-cycle pulse: KILL frame received

    input  logic                order_valid,
    output logic                order_ready,
    input  logic                is_hedge,
    input  logic        [2:0]   contract,
    input  logic                side,
    input  logic        [15:0]  qty,
    input  logic        [17:0]  price,

    input  logic        [31:0]  snap_flip_us,
    input  logic        [15:0]  ref_seq,
    input  logic        [15:0]  feed_seq,
    input  logic                feed_bad,
    input  logic        [20:0]  S,
    input  logic        [20:0]  S_ref,
    input  logic        [17:0]  q_bid,
    input  logic        [17:0]  q_ask,
    input  logic        [11:0]  q_bsz,
    input  logic        [11:0]  q_asz,
    input  logic        [23:0]  mp_q4,
    input  logic        [17:0]  theo_0_c,
    input  logic signed [17:0]  delta_adj_c,
    input  logic        [17:0]  gamma_c_c,
    input  logic        [15:0]  vega_c,
    input  logic signed [8:0]   pos_c,
    input  logic signed [16:0]  sh,
    input  logic signed [31:0]  sum_delta,
    input  logic signed [31:0]  sum_gamma,
    input  logic signed [31:0]  sum_vega,
    input  logic signed [47:0]  equity_x6400,
    input  logic        [31:0]  hb_last_us,
    input  logic                kill,

    output logic                result_valid,
    output logic                approve,
    output logic        [3:0]   reason_code,
    output logic        [14:0]  fail_mask,
    output logic                reduce_only,
    output logic                blocked,
    output logic        [4:0]   latch_req,          // {R15 KILL frame, R14 (0), R13, R12, R03}
    output logic                reject_pulse,       // to arming (LEDs)
    output logic        [3:0]   reject_code,
    output logic        [4:0]   tokens,
    output logic        [3:0]   orders_in_event,
    output logic                r13_latched,
    output logic        [2:0]   result_contract,    // order identity for the position keeper
    output logic                result_is_hedge
);
    // ------------------------------------------------------------ one order in flight
    localparam int unsigned BUSY = 3 + SETTLE_CYCLES;
    logic [2:0] busy;
    wire accept = order_valid & order_ready;
    assign order_ready = (busy == 3'd0);
    always_ff @(posedge clk) begin
        if (!rst_n)          busy <= 3'd0;
        else if (accept)     busy <= 3'(BUSY);
        else if (busy != 0)  busy <= busy - 3'd1;
    end

    // identity of the order in flight (fields are only meaningful while it is in flight)
    always_ff @(posedge clk) begin
        if (accept) begin
            result_contract <= contract;
            result_is_hedge <= is_hedge;
        end
    end

    // ------------------------------------------------------------ R13 latch (G8)
    wire r13_cond = equity_x6400 < -48'sd12_800_000;
    logic tripped_d;
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            tripped_d   <= 1'b0;
            r13_latched <= 1'b0;
        end else begin
            tripped_d <= tripped;
            if (r13_cond)                    r13_latched <= 1'b1;
            else if (tripped_d && !tripped)  r13_latched <= 1'b0;   // arming cleared
        end
    end

    // ------------------------------------------------------------ core
    gate_core u_core (
        .clk, .rst_n, .in_valid(accept),
        .is_hedge, .side, .qty, .price,
        .now_us, .snap_flip_us, .ref_seq, .feed_seq, .feed_bad, .S, .S_ref,
        .q_bid, .q_ask, .q_bsz, .q_asz, .mp_q4, .theo_0_c, .delta_adj_c, .gamma_c_c, .vega_c,
        .pos_c, .sh, .sum_delta, .sum_gamma, .sum_vega, .equity_x6400, .hb_last_us, .kill,
        .tokens, .orders_in_event, .r13_force(r13_latched),
        .out_valid(result_valid), .reason(reason_code), .fail_mask, .reduce_only);

    assign blocked      = ~armed | tripped;
    assign approve      = result_valid & ~blocked & (reason_code == 4'd0);
    assign reject_pulse = result_valid & ~approve;
    assign reject_code  = reason_code;

    // ------------------------------------------------------------ token bucket (R12, G9)
    localparam int RW = $clog2(REFILL_US);
    logic [RW-1:0] refill_cnt;
    wire refill = tick_1us && (refill_cnt == RW'(REFILL_US - 1));
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            refill_cnt <= '0;
            tokens     <= 5'(TOKENS_MAX);
        end else begin
            if (tick_1us) refill_cnt <= refill ? '0 : refill_cnt + 1'b1;
            if (refill && !approve) begin
                if (tokens != 5'(TOKENS_MAX)) tokens <= tokens + 5'd1;
            end else if (approve && !refill) begin
                tokens <= tokens - 5'd1;                 // approve implies tokens > 0 (R12)
            end
        end
    end

    // ------------------------------------------------------------ orders in event (G10)
    always_ff @(posedge clk) begin
        if (!rst_n)                                  orders_in_event <= 4'd0;
        else if (event_start)                        orders_in_event <= approve ? 4'd1 : 4'd0;
        else if (approve && orders_in_event != 4'hF) orders_in_event <= orders_in_event + 4'd1;
    end

    // ------------------------------------------------------------ latch requests (G11)
    assign latch_req = {kill_frame, 1'b0, r13_cond | r13_latched,
                        result_valid & fail_mask[11], feed_bad};
endmodule
