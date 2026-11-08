// io_shock.sv - demo shock generator, stale market-maker refresh and fill emulation.
// SIMULATED market: every quote this block emits is synthetic demo data.
//
// Target: Quartus Prime Lite, 10M50DAF484C7G, single 50 MHz clock. No latches, no initial blocks.
//
// Flow (one shock at a time):
//   green_press & armed & ~tripped & idle & SW[7:0] != 0
//     -> capture SW[8:0], copy the needed shock-table rows, draw the LFSR, compute W and
//        the 8 refresh offsets (LOAD, ~45 cycles)
//     -> emit the base quotes: 1 underlying + 8 options (BASE)
//     -> 1,000 us after the press, emit the shocked underlying quote (SHOCK). When the
//        parser accepts that word, that is T0 (t0_pulse, latency counter starts).
//     -> contract c refreshes at T0 + off[c], emitting its shocked option quote (ACTIVE)
//     -> back to IDLE after all 8 refreshes.
//   Orders: an option order fills in full if it arrives while its contract has not yet
//   refreshed; otherwise the fill quantity is 0. A fill-report word goes out either way.
//
// ASSUMPTIONS (PROPOSED - not in CLAUDE.md):
//  IO1. green_press is the debounced one-cycle pulse from fpga/arming (green button).
//  IO2. Shock-table row size = 0 holds the base option quotes (the laptop loads the same
//       base row for both directions). A shock of size 0 therefore does nothing.
//  IO3. 0x13 load block payload: start address (2 bytes, big-endian) then N 16-bit
//       big-endian words written to consecutive addresses. The UART frame decoder (not in
//       this block) drives load_we/load_addr/load_data, one word per strobe. Address map:
//         0x0000-0x1FFF  shock table, addr = {dir, size[7:0], contract[2:0], is_ask}
//         0x2000/0x2001  underlying base bid [17:16] / [15:0], cents   (reset 14713)
//         0x2002/0x2003  underlying base ask [17:16] / [15:0], cents   (reset 14714)
//         0x2004/0x2005  underlying bid / ask size                      (reset 100 / 100)
//         0x2006/0x2007  option quote bid / ask size                    (reset 10 / 10)
//         0x2008         stale_median_us                                (reset 100)
//         0x2009         slider_scale, Q8 (256 = 1.0)                    (reset 256)
//       The table RAM is not initialised (no initial blocks): load it before the first shock.
//  IO4. slider_scale is a register loaded over UART (there is no slider input defined).
//  IO5. jitter = (128 + r) / 256 with r the 8-bit maximal LFSR state (1..255), i.e.
//       0.504..1.496. u_c = r_c / 256. The 0.2 stagger is applied as 51/256 (0.1992).
//       W_us = (stale_median_us * slider_q8 * jitter_q8) >> 16;
//       off_c = (W_us * (65536 - 51 * r_c)) >> 16 (truncating). The LFSR steps once per
//       draw: draw 0 is the jitter, draws 1..8 are u_0..u_7.
//  IO6. Fill reports: type 3 (buy) / 4 (sell), id = contract, bid = order price,
//       ask = filled quantity, sizes 0. An option order fills only between T0 and its
//       contract's refresh time; before T0 or outside a shock the quantity is 0.
//       Hedge orders (id 8) always fill in full at the order price.
//  IO7. Down shocks clamp the underlying bid/ask at 0 (R05 then rejects that quote).
//  IO8. Output words carry seq from this block's own 16-bit counter (wraps) and
//       ts_us = now_us when the word is built; CRC-8 poly 0x07, init 0, MSB first over
//       bits 127..8, the same as golden.word_crc.
//  IO9. Latency = clock cycles from the T0 word handshake to the first order_valid,
//       times 20 ns, saturating at 999,999 ns. HEX shows dashes until a shock gets an
//       order, and stays dashed if a shock ends without one.
//  IO10. One word per cycle; priority fill > refresh > shocked underlying > base. Fill
//       reports queue in a 16-deep FIFO (fill_overflow flags a drop).

module io_shock #(
    parameter int          PRESS_TO_T0_US = 1000,
    parameter int          CLK_NS         = 20,
    parameter logic [7:0]  LFSR_SEED      = 8'h5A
) (
    input  logic         clk,
    input  logic         rst_n,
    input  logic [31:0]  now_us,          // shared free-running microsecond counter

    input  logic [8:0]   sw,              // raw SW[8:0]: [7:0] size in cents, [8] 1 = up
    input  logic         green_press,     // IO1
    input  logic         armed,
    input  logic         tripped,

    input  logic         load_we,         // IO3
    input  logic [13:0]  load_addr,
    input  logic [15:0]  load_data,

    input  logic         ord_valid,       // approved order from the gate
    input  logic         ord_is_hedge,
    input  logic [2:0]   ord_contract,
    input  logic         ord_side,        // 0 buy, 1 sell
    input  logic [15:0]  ord_qty,
    input  logic [17:0]  ord_price,

    output logic         word_valid,      // message word into the parser
    output logic [127:0] word,
    input  logic         word_ready,

    output logic         t0_pulse,
    output logic         shock_busy,
    output logic         fill_overflow,
    output logic [7:0]   hex0, hex1, hex2, hex3, hex4, hex5,   // active low, bit 7 = DP

    // decode / debug
    output logic         sw_dir,
    output logic [7:0]   sw_size,
    output logic [8:0]   dbg_jitter_q8,
    output logic [7:0]   dbg_u [8],
    output logic [24:0]  dbg_w_us,
    output logic [24:0]  dbg_off_us [8],
    output logic [19:0]  dbg_latency_ns
);
    // ------------------------------------------------------------ reset, switch sync
    logic [1:0] rst_sync;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) rst_sync <= 2'b00;
        else        rst_sync <= {rst_sync[0], 1'b1};
    wire rst = ~rst_sync[1];

    logic [8:0] sw_ff0, sw_ff1;
    always_ff @(posedge clk) begin
        sw_ff0 <= sw;
        sw_ff1 <= sw_ff0;
    end
    assign sw_dir  = sw_ff1[8];
    assign sw_size = sw_ff1[7:0];

    // ------------------------------------------------------------ CRC-8 (IO8)
    function automatic logic [7:0] crc8_120(input logic [119:0] d);
        logic [7:0] c;
        logic       fb;
        c = 8'h00;
        for (int i = 119; i >= 0; i--) begin
            fb = c[7] ^ d[i];
            c  = {c[6:0], 1'b0} ^ (fb ? 8'h07 : 8'h00);
        end
        return c;
    endfunction

    function automatic logic [127:0] mk_word(input logic [7:0] typ, input logic [15:0] seq,
                                             input logic [31:0] ts, input logic [3:0] id,
                                             input logic [17:0] bid, input logic [17:0] ask,
                                             input logic [11:0] bsz, input logic [11:0] asz);
        logic [119:0] body;
        body = {typ, seq, ts, id, bid, ask, bsz, asz};
        return {body, crc8_120(body)};
    endfunction

    // ------------------------------------------------------------ table RAM + config (IO3)
    logic [15:0] tbl [0:8191];
    logic [12:0] rd_addr;
    logic [15:0] tbl_q;
    always_ff @(posedge clk) begin
        if (load_we && !load_addr[13]) tbl[load_addr[12:0]] <= load_data;
        tbl_q <= tbl[rd_addr];
    end

    logic [17:0] und_bid, und_ask;
    logic [11:0] und_bsz, und_asz, opt_bsz, opt_asz;
    logic [15:0] stale_med_us, slider_q8;
    always_ff @(posedge clk) begin
        if (rst) begin
            und_bid <= 18'd14713;  und_ask <= 18'd14714;
            und_bsz <= 12'd100;    und_asz <= 12'd100;
            opt_bsz <= 12'd10;     opt_asz <= 12'd10;
            stale_med_us <= 16'd100;
            slider_q8    <= 16'd256;
        end else if (load_we && load_addr[13]) begin
            unique case (load_addr[3:0])
                4'h0: und_bid[17:16] <= load_data[1:0];
                4'h1: und_bid[15:0]  <= load_data;
                4'h2: und_ask[17:16] <= load_data[1:0];
                4'h3: und_ask[15:0]  <= load_data;
                4'h4: und_bsz <= load_data[11:0];
                4'h5: und_asz <= load_data[11:0];
                4'h6: opt_bsz <= load_data[11:0];
                4'h7: opt_asz <= load_data[11:0];
                4'h8: stale_med_us <= load_data;
                4'h9: slider_q8    <= load_data;
                default: ;
            endcase
        end
    end

    // ------------------------------------------------------------ LFSR (IO5)
    logic       lfsr_step;
    logic [7:0] lfsr_r;
    logic [8:0] lfsr_jit;
    shock_lfsr #(.SEED(LFSR_SEED)) u_lfsr (.clk, .rst, .step(lfsr_step), .r(lfsr_r), .jitter_q8(lfsr_jit));

    // ------------------------------------------------------------ shock FSM
    typedef enum logic [2:0] {S_IDLE, S_LOAD, S_CALC, S_BASE, S_WAIT, S_SHOCK, S_ACTIVE} state_t;
    state_t state;

    logic        cap_dir;
    logic [7:0]  cap_size;
    logic [31:0] t_press, t0;
    logic [5:0]  ld_cnt;
    logic [3:0]  calc_cnt;
    logic [15:0] base_bid [8], base_ask [8], sh_bid [8], sh_ask [8];
    logic [7:0]  u_r [8];
    logic [8:0]  jit_q8;
    logic [31:0] m1;
    logic [24:0] w_us;
    logic [24:0] off_us [8];
    logic [7:0]  done_c;
    logic [8:0]  base_pend;            // bit 8 = underlying, bits 7..0 = options
    logic        shock_pend;
    logic [7:0]  ref_pend;
    logic        out_is_shock;
    logic        t0_accept;

    wire start = green_press && armed && !tripped && (state == S_IDLE) && (sw_size != 8'd0);
    wire [31:0] since_t0 = now_us - t0;
    wire [4:0]  ld_k     = 5'(ld_cnt - 6'd1);                       // table entry landing this cycle
    wire [2:0]  calc_c   = 3'(calc_cnt - 4'd2);                     // contract whose offset is computed
    wire [16:0] calc_fac = 17'd65536 - 17'(u_r[calc_c]) * 17'd51;   // 1 - 0.2*u_c, Q16 (IO5)

    // load sequencing: ld_cnt 0..31 issues reads {row, c, is_ask}; data lands one cycle later
    wire        ld_row   = ld_cnt[4];
    wire [2:0]  ld_c     = ld_cnt[3:1];
    wire        ld_ask   = ld_cnt[0];
    assign rd_addr   = {cap_dir, ld_row ? cap_size : 8'd0, ld_c, ld_ask};
    assign lfsr_step = (state == S_LOAD) && (ld_cnt < 6'd9);

    // word arbitration (declared here, driven below)
    logic        fifo_empty, fifo_pop;
    logic [7:0]  ff_type;
    logic [3:0]  ff_id;
    logic [17:0] ff_price, ff_qty;
    logic [2:0]  ref_sel, base_sel;
    logic        load_out;
    logic [15:0] seq_cnt;

    always_comb begin
        ref_sel = 3'd0;
        for (int c = 7; c >= 0; c--) if (ref_pend[c]) ref_sel = 3'(c);
        base_sel = 3'd0;
        for (int c = 7; c >= 0; c--) if (base_pend[c]) base_sel = 3'(c);
    end
    assign load_out = !word_valid || word_ready;
    assign fifo_pop = load_out && !fifo_empty;
    assign t0_accept = word_valid && word_ready && out_is_shock;

    logic [17:0] shk_bid, shk_ask;
    always_comb begin
        if (cap_dir) begin
            shk_bid = und_bid + 18'(cap_size);
            shk_ask = und_ask + 18'(cap_size);
        end else begin
            shk_bid = (und_bid > 18'(cap_size)) ? und_bid - 18'(cap_size) : 18'd0;   // IO7
            shk_ask = (und_ask > 18'(cap_size)) ? und_ask - 18'(cap_size) : 18'd0;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= S_IDLE;
            cap_dir <= 1'b0; cap_size <= 8'd0; t_press <= '0; t0 <= '0;
            ld_cnt <= '0; calc_cnt <= '0; jit_q8 <= 9'd256; m1 <= '0; w_us <= '0;
            done_c <= '0; base_pend <= '0; shock_pend <= 1'b0; ref_pend <= '0;
            word_valid <= 1'b0; word <= '0; out_is_shock <= 1'b0; seq_cnt <= '0;
            for (int c = 0; c < 8; c++) begin
                base_bid[c] <= '0; base_ask[c] <= '0; sh_bid[c] <= '0; sh_ask[c] <= '0;
                u_r[c] <= '0; off_us[c] <= '0;
            end
        end else begin
            // ---------------- output register: one word per cycle (IO10)
            if (load_out) begin
                word_valid   <= 1'b1;
                out_is_shock <= 1'b0;
                seq_cnt      <= seq_cnt + 16'd1;
                if (!fifo_empty) begin
                    word <= mk_word(ff_type, seq_cnt, now_us, ff_id, ff_price, ff_qty, 12'd0, 12'd0);
                end else if (ref_pend != 8'd0) begin
                    word <= mk_word(8'd2, seq_cnt, now_us, {1'b0, ref_sel},
                                    18'(sh_bid[ref_sel]), 18'(sh_ask[ref_sel]), opt_bsz, opt_asz);
                    ref_pend[ref_sel] <= 1'b0;
                end else if (shock_pend) begin
                    word <= mk_word(8'd1, seq_cnt, now_us, 4'd8, shk_bid, shk_ask, und_bsz, und_asz);
                    shock_pend   <= 1'b0;
                    out_is_shock <= 1'b1;
                end else if (base_pend[8]) begin
                    word <= mk_word(8'd1, seq_cnt, now_us, 4'd8, und_bid, und_ask, und_bsz, und_asz);
                    base_pend[8] <= 1'b0;
                end else if (base_pend[7:0] != 8'd0) begin
                    word <= mk_word(8'd2, seq_cnt, now_us, {1'b0, base_sel},
                                    18'(base_bid[base_sel]), 18'(base_ask[base_sel]), opt_bsz, opt_asz);
                    base_pend[base_sel] <= 1'b0;
                end else begin
                    word_valid <= 1'b0;
                    seq_cnt    <= seq_cnt;
                end
            end

            // ---------------- shock sequence
            unique case (state)
                S_IDLE: if (start) begin
                    cap_dir  <= sw_dir;
                    cap_size <= sw_size;
                    t_press  <= now_us;
                    ld_cnt   <= '0;
                    state    <= S_LOAD;
                end
                S_LOAD: begin
                    // LFSR draws: step at ld_cnt 0..8, state visible the cycle after
                    if (ld_cnt == 6'd1)                       jit_q8 <= lfsr_jit;
                    if (ld_cnt >= 6'd2 && ld_cnt <= 6'd9)     u_r[ld_cnt - 6'd2] <= lfsr_r;
                    if (ld_cnt >= 6'd1) begin
                        if (!ld_k[4]) begin
                            if (ld_k[0]) base_ask[ld_k[3:1]] <= tbl_q; else base_bid[ld_k[3:1]] <= tbl_q;
                        end else begin
                            if (ld_k[0]) sh_ask[ld_k[3:1]] <= tbl_q;   else sh_bid[ld_k[3:1]] <= tbl_q;
                        end
                    end
                    if (ld_cnt == 6'd32) begin
                        calc_cnt <= '0;
                        state    <= S_CALC;
                    end else begin
                        ld_cnt <= ld_cnt + 6'd1;
                    end
                end
                S_CALC: begin
                    calc_cnt <= calc_cnt + 4'd1;
                    if (calc_cnt == 4'd0) m1   <= 32'(stale_med_us) * 32'(slider_q8);
                    if (calc_cnt == 4'd1) w_us <= 25'((41'(m1) * 41'(jit_q8)) >> 16);
                    if (calc_cnt >= 4'd2 && calc_cnt <= 4'd9)
                        off_us[calc_c] <= 25'((42'(w_us) * 42'(calc_fac)) >> 16);
                    if (calc_cnt == 4'd9) begin
                        base_pend <= 9'h1FF;
                        state     <= S_BASE;
                    end
                end
                S_BASE: if (base_pend == 9'd0) state <= S_WAIT;
                S_WAIT: if (now_us - t_press >= 32'(PRESS_TO_T0_US)) begin
                    shock_pend <= 1'b1;
                    state      <= S_SHOCK;
                end
                S_SHOCK: if (t0_accept) begin
                    t0     <= now_us;
                    done_c <= '0;
                    state  <= S_ACTIVE;
                end
                S_ACTIVE: begin
                    for (int c = 0; c < 8; c++)
                        if (!done_c[c] && since_t0 >= 32'(off_us[c])) begin
                            done_c[c]   <= 1'b1;
                            ref_pend[c] <= 1'b1;
                        end
                    if (done_c == 8'hFF && ref_pend == 8'd0) state <= S_IDLE;
                end
                default: state <= S_IDLE;
            endcase
        end
    end

    assign shock_busy = (state != S_IDLE);
    assign t0_pulse   = t0_accept;
    assign dbg_jitter_q8 = jit_q8;
    assign dbg_w_us      = w_us;
    always_comb for (int c = 0; c < 8; c++) begin
        dbg_u[c]      = u_r[c];
        dbg_off_us[c] = off_us[c];
    end

    // ------------------------------------------------------------ fills (IO6)
    wire         opt_live = (state == S_ACTIVE) && !done_c[ord_contract]
                            && (since_t0 < 32'(off_us[ord_contract]));
    wire [17:0]  fill_qty = (ord_is_hedge || opt_live) ? 18'(ord_qty) : 18'd0;
    wire [47:0]  fifo_din = {ord_side ? 8'd4 : 8'd3, ord_is_hedge ? 4'd8 : {1'b0, ord_contract},
                             ord_price, fill_qty};
    logic [47:0] fifo_mem [16];
    logic [4:0]  wr_ptr, rd_ptr;
    assign fifo_empty = (wr_ptr == rd_ptr);
    wire   fifo_full  = (wr_ptr[3:0] == rd_ptr[3:0]) && (wr_ptr[4] != rd_ptr[4]);
    assign {ff_type, ff_id, ff_price, ff_qty} = fifo_mem[rd_ptr[3:0]];

    always_ff @(posedge clk) begin
        if (rst) begin
            wr_ptr <= '0; rd_ptr <= '0; fill_overflow <= 1'b0;
        end else begin
            if (ord_valid) begin
                if (!fifo_full) begin
                    fifo_mem[wr_ptr[3:0]] <= fifo_din;
                    wr_ptr <= wr_ptr + 5'd1;
                end else begin
                    fill_overflow <= 1'b1;
                end
            end
            if (fifo_pop) rd_ptr <= rd_ptr + 5'd1;
        end
    end

    // ------------------------------------------------------------ latency (IO9)
    localparam int LAT_MAX_NS = 999_999;
    logic        lat_run, have_lat;
    logic [15:0] lat_cyc;
    logic [19:0] lat_ns;
    logic        dd_start;
    wire  [31:0] lat_ns_raw = (32'(lat_cyc) + 32'd1) * 32'(CLK_NS);
    always_ff @(posedge clk) begin
        if (rst) begin
            lat_run <= 1'b0; have_lat <= 1'b0; lat_cyc <= '0; lat_ns <= '0; dd_start <= 1'b0;
        end else begin
            dd_start <= 1'b0;
            if (t0_accept) begin
                lat_run  <= 1'b1;
                have_lat <= 1'b0;
                lat_cyc  <= '0;
            end else if (lat_run) begin
                if (ord_valid) begin
                    lat_ns   <= (lat_ns_raw > LAT_MAX_NS) ? 20'(LAT_MAX_NS) : 20'(lat_ns_raw);
                    lat_run  <= 1'b0;
                    have_lat <= 1'b1;
                    dd_start <= 1'b1;
                end else if (state == S_IDLE) begin
                    lat_run <= 1'b0;                       // shock ended with no order
                end else if (lat_cyc != 16'hFFFF) begin
                    lat_cyc <= lat_cyc + 16'd1;
                end
            end
        end
    end
    assign dbg_latency_ns = lat_ns;

    logic [23:0] bcd;
    logic        dd_done;
    bin2bcd_dd #(.BIN_W(20), .DIGITS(6)) u_dd (.clk, .rst, .start(dd_start), .bin(lat_ns),
                                              .bcd(bcd), .done(dd_done));

    // show only after a conversion finishes; dashes otherwise (IO9)
    logic show;
    always_ff @(posedge clk)
        if (rst || t0_accept) show <= 1'b0;
        else if (dd_done)     show <= have_lat;

    function automatic logic [7:0] seg7(input logic [3:0] d);   // active low, gfedcba, DP off
        unique case (d)
            4'd0: return 8'hC0;  4'd1: return 8'hF9;  4'd2: return 8'hA4;  4'd3: return 8'hB0;
            4'd4: return 8'h99;  4'd5: return 8'h92;  4'd6: return 8'h82;  4'd7: return 8'hF8;
            4'd8: return 8'h80;  4'd9: return 8'h90;
            default: return 8'hFF;
        endcase
    endfunction

    // digit i is blanked when it and every digit above it are 0 (digit 0 always shown)
    logic [7:0] seg [6];
    logic [5:0] nz_above;                      // nz_above[i] = some digit at index >= i is nonzero
    always_comb begin
        nz_above[5] = (bcd[23:20] != 4'd0);
        for (int i = 4; i >= 0; i--) nz_above[i] = nz_above[i+1] || (bcd[i*4 +: 4] != 4'd0);
        for (int i = 0; i < 6; i++) begin
            if (!show)                       seg[i] = 8'hBF;              // dash (segment g)
            else if (!nz_above[i] && i != 0) seg[i] = 8'hFF;              // blank leading zero
            else                             seg[i] = seg7(bcd[i*4 +: 4]);
        end
    end
    assign {hex5, hex4, hex3, hex2, hex1, hex0} = {seg[5], seg[4], seg[3], seg[2], seg[1], seg[0]};
endmodule


// 8-bit maximal Galois LFSR (x^8 + x^6 + x^5 + x^4 + 1), steps once per `step`.
// r is never 0, so jitter_q8 = 128 + r is 129..383, i.e. 0.504..1.496 (IO5).
module shock_lfsr #(
    parameter logic [7:0] SEED = 8'h5A
) (
    input  logic       clk,
    input  logic       rst,
    input  logic       step,
    output logic [7:0] r,
    output logic [8:0] jitter_q8
);
    always_ff @(posedge clk)
        if (rst)       r <= (SEED == 8'h00) ? 8'h01 : SEED;
        else if (step) r <= {1'b0, r[7:1]} ^ (r[0] ? 8'hB8 : 8'h00);
    assign jitter_q8 = 9'd128 + 9'(r);
endmodule


// Sequential double dabble: BIN_W-bit binary to DIGITS BCD digits in BIN_W cycles.
module bin2bcd_dd #(
    parameter int BIN_W  = 20,
    parameter int DIGITS = 6
) (
    input  logic                  clk,
    input  logic                  rst,
    input  logic                  start,
    input  logic [BIN_W-1:0]      bin,
    output logic [DIGITS*4-1:0]   bcd,
    output logic                  done
);
    logic [BIN_W-1:0]    sh;
    logic [DIGITS*4-1:0] acc;
    logic [$clog2(BIN_W+1)-1:0] n;
    logic busy;
    logic [DIGITS*4-1:0] a;                    // acc after the add-3 step

    always_comb
        for (int i = 0; i < DIGITS; i++)
            a[i*4 +: 4] = (acc[i*4 +: 4] >= 4'd5) ? acc[i*4 +: 4] + 4'd3 : acc[i*4 +: 4];

    always_ff @(posedge clk) begin
        if (rst) begin
            sh <= '0; acc <= '0; bcd <= '0; n <= '0; busy <= 1'b0; done <= 1'b0;
        end else begin
            done <= 1'b0;
            if (start) begin
                sh <= bin; acc <= '0; n <= '0; busy <= 1'b1;
            end else if (busy) begin
                acc <= {a[DIGITS*4-2:0], sh[BIN_W-1]};
                sh  <= {sh[BIN_W-2:0], 1'b0};
                n   <= n + 1'b1;
                if (n == BIN_W - 1) begin
                    bcd  <= {a[DIGITS*4-2:0], sh[BIN_W-1]};
                    busy <= 1'b0;
                    done <= 1'b1;
                end
            end
        end
    end
endmodule
