// arming.sv - arming, latching trips, operator I/O for the Latency Arena gate.
// Target: Intel MAX 10 10M50DAF484C7G (DE10-Lite), Quartus Prime Lite, one 50 MHz clock.
// No latches, no initial blocks. All timing counts the 1 us tick.
//
// ASSUMPTIONS (see README in the task summary; change here if wrong):
//  AR1. rst_n is an active-low reset (power-on reset or a board button). It is
//       asserted asynchronously and released synchronously inside this module.
//  AR2. KEY0 and the green button are DE10-Lite push buttons: active low (0 = pressed).
//  AR3. SW9 (ARM) is 2-FF synchronized only (the spec debounces only KEY0 and the green
//       button). Its synchronizer resets to 1, so an ARM switch that is already up at
//       power-on never produces a rising edge.
//  AR4. Green button: purpose is not defined in CLAUDE.md. It is synchronized,
//       debounced and exported as a one-cycle press pulse (green_press) only.
//  AR5. The kill pin is normally closed: high = pressed or unplugged. It is synchronized
//       only (no debounce), so the first synchronized high latches R15.
//  AR6. latch_req bit order {R15, R14, R13, R12, R03}. Several causes in the same cycle:
//       the lowest code wins (same priority as the gate). Once latched, the cause is frozen.
//  AR7. The heartbeat watchdog starts at the first valid heartbeat after reset or after a
//       latch clear. It trips R14 when more than HB_TIMEOUT_US ticks pass with no valid
//       heartbeat (same "> 150,000 us" rule as the gate). Arming needs >= 2 heartbeats
//       counted since reset or since the last clear.
//  AR8. armed falls when ARM goes low or on any trip. Losing book_ready or snapshot_ok
//       while armed does not disarm here; the gate blocks orders with R01/R02.
//  AR9. Clearing (KEY0 press while ARM is low) clears the latch, relay and reason, and
//       restarts the heartbeat count. A cause that is still present (kill high) latches
//       again on the next cycle.
//  AR10. Soft rejects arrive as reject_pulse + reject_code from the gate. LEDR[3:0]
//       shows the latched reason when tripped, else the last reject code for HOLD_US,
//       else 0. LEDR4 is unused (0).
//  AR11. Software frames have no path to arm or clear: there is deliberately no such
//       port. A KILL frame can only trip, through latch_req[4].

module arming #(
    parameter int DEBOUNCE_US   = 10_000,      // 10 ms stable time for KEY0 and green
    parameter int HB_TIMEOUT_US = 150_000,     // R14
    parameter int HOLD_US       = 1_000_000    // buzzer and soft-reject LED hold
) (
    input  logic       clk,                // 50 MHz
    input  logic       rst_n,              // AR1
    input  logic       tick_1us,           // one-cycle pulse every 1 us, synchronous to clk

    // external pins (asynchronous)
    input  logic       arm_sw,             // SW9, high = ARM
    input  logic       key0_n,             // KEY0, active low
    input  logic       green_n,            // green button, active low (AR4)
    input  logic       kill_in,            // NC kill loop, high = pressed or unplugged

    // internal, synchronous to clk
    input  logic       hb_valid,           // one-cycle pulse per valid heartbeat frame
    input  logic       book_ready,         // all 9 instruments seen
    input  logic       snapshot_ok,        // committed snapshot passes R01 and R02
    input  logic [4:0] latch_req,          // {R15 KILL frame, R14, R13, R12, R03} from the gate
    input  logic       reject_pulse,       // soft reject from the gate (AR10)
    input  logic [3:0] reject_code,

    output logic       armed,
    output logic       tripped,
    output logic [3:0] latched_reason,     // 0 when not tripped
    output logic       relay,              // high = tripped
    output logic       buzzer_n,           // active low
    output logic       hb_ok,
    output logic       green_press,        // AR4
    output logic [9:0] ledr
);

    // ------------------------------------------------------------ reset synchronizer
    logic [1:0] rst_sync;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) rst_sync <= 2'b00;
        else        rst_sync <= {rst_sync[0], 1'b1};
    wire rst = ~rst_sync[1];

    // ------------------------------------------------------------ 2-FF synchronizers
    logic [1:0] arm_ff, key_ff, grn_ff, kill_ff;
    always_ff @(posedge clk) begin
        if (rst) begin
            arm_ff  <= 2'b11;              // AR3: assume ARM up until seen low
            key_ff  <= 2'b11;              // released
            grn_ff  <= 2'b11;
            kill_ff <= 2'b00;
        end else begin
            arm_ff  <= {arm_ff[0],  arm_sw};
            key_ff  <= {key_ff[0],  key0_n};
            grn_ff  <= {grn_ff[0],  green_n};
            kill_ff <= {kill_ff[0], kill_in};
        end
    end
    wire arm_s  = arm_ff[1];
    wire kill_s = kill_ff[1];

    // ------------------------------------------------------------ debounce (10 ms stable)
    logic key_db_n, grn_db_n;
    arming_debounce #(.STABLE_US(DEBOUNCE_US)) u_db_key (
        .clk, .rst, .tick_1us, .din(key_ff[1]), .dout(key_db_n));
    arming_debounce #(.STABLE_US(DEBOUNCE_US)) u_db_grn (
        .clk, .rst, .tick_1us, .din(grn_ff[1]), .dout(grn_db_n));

    logic key_db_n_d, grn_db_n_d, arm_d;
    always_ff @(posedge clk) begin
        if (rst) begin
            key_db_n_d <= 1'b1;
            grn_db_n_d <= 1'b1;
            arm_d      <= 1'b1;
        end else begin
            key_db_n_d <= key_db_n;
            grn_db_n_d <= grn_db_n;
            arm_d      <= arm_s;
        end
    end
    wire key_press = key_db_n_d & ~key_db_n;     // falling edge = press
    wire arm_rise  = ~arm_d & arm_s;
    assign green_press = grn_db_n_d & ~grn_db_n;

    wire clear = key_press & ~arm_s & tripped;   // only while ARM is low

    // ------------------------------------------------------------ heartbeat watchdog
    localparam int HBW = $clog2(HB_TIMEOUT_US + 2);
    logic [HBW-1:0] hb_timer;
    logic           hb_seen;
    logic [1:0]     hb_cnt;                      // saturates at 2

    always_ff @(posedge clk) begin
        if (rst || clear) begin
            hb_timer <= '0;
            hb_seen  <= 1'b0;
            hb_cnt   <= 2'd0;
        end else if (hb_valid) begin
            hb_timer <= '0;
            hb_seen  <= 1'b1;
            if (hb_cnt != 2'd2) hb_cnt <= hb_cnt + 2'd1;
        end else if (hb_seen && tick_1us && hb_timer <= HBW'(HB_TIMEOUT_US)) begin
            hb_timer <= hb_timer + 1'b1;         // stops at HB_TIMEOUT_US + 1
        end
    end
    wire hb_loss = hb_seen & (hb_timer > HBW'(HB_TIMEOUT_US));
    assign hb_ok = hb_seen & ~hb_loss;

    // ------------------------------------------------------------ latch, first cause wins
    wire [4:0] req = {latch_req[4] | kill_s, latch_req[3] | hb_loss, latch_req[2:0]};
    logic [3:0] req_code;
    always_comb begin
        if      (req[0]) req_code = 4'd3;
        else if (req[1]) req_code = 4'd12;
        else if (req[2]) req_code = 4'd13;
        else if (req[3]) req_code = 4'd14;
        else if (req[4]) req_code = 4'd15;
        else             req_code = 4'd0;
    end
    wire trip_now = (req != 5'd0) & ~tripped;

    always_ff @(posedge clk) begin
        if (rst) begin
            tripped        <= 1'b0;
            latched_reason <= 4'd0;
        end else if (clear) begin
            tripped        <= 1'b0;
            latched_reason <= 4'd0;
        end else if (trip_now) begin
            tripped        <= 1'b1;
            latched_reason <= req_code;
        end
    end
    assign relay = tripped;

    // ------------------------------------------------------------ arming
    wire ready = book_ready & snapshot_ok & (hb_cnt == 2'd2) & ~tripped & ~trip_now;
    always_ff @(posedge clk) begin
        if (rst)                         armed <= 1'b0;
        else if (!arm_s || tripped || trip_now) armed <= 1'b0;
        else if (arm_rise && ready)      armed <= 1'b1;
    end

    // ------------------------------------------------------------ buzzer and reject hold
    localparam int HW = $clog2(HOLD_US + 1);
    logic [HW-1:0] buzz_cnt, rej_cnt;
    logic [3:0]    rej_code_r;
    always_ff @(posedge clk) begin
        if (rst) begin
            buzz_cnt   <= '0;
            rej_cnt    <= '0;
            rej_code_r <= 4'd0;
        end else begin
            if (trip_now)                         buzz_cnt <= HW'(HOLD_US);
            else if (tick_1us && buzz_cnt != '0)  buzz_cnt <= buzz_cnt - 1'b1;

            if (reject_pulse) begin
                rej_cnt    <= HW'(HOLD_US);
                rej_code_r <= reject_code;
            end else if (tick_1us && rej_cnt != '0) begin
                rej_cnt    <= rej_cnt - 1'b1;
            end
        end
    end
    assign buzzer_n = (buzz_cnt == '0);

    // ------------------------------------------------------------ LEDs
    logic [3:0] led_code;
    always_comb begin
        if (tripped)            led_code = latched_reason;
        else if (rej_cnt != '0) led_code = rej_code_r;
        else                    led_code = 4'd0;
    end
    assign ledr = {tripped, armed, hb_ok, book_ready, snapshot_ok, 1'b0, led_code};

endmodule


// Debouncer: dout follows din once din has differed from dout for STABLE_US ticks.
module arming_debounce #(
    parameter int STABLE_US = 10_000
) (
    input  logic clk,
    input  logic rst,
    input  logic tick_1us,
    input  logic din,          // already synchronized
    output logic dout
);
    localparam int W = $clog2(STABLE_US + 1);
    logic [W-1:0] cnt;
    always_ff @(posedge clk) begin
        if (rst) begin
            dout <= 1'b1;      // buttons are active low; released at reset
            cnt  <= '0;
        end else if (din == dout) begin
            cnt  <= '0;
        end else if (tick_1us) begin
            if (cnt == W'(STABLE_US - 1)) begin
                dout <= din;
                cnt  <= '0;
            end else begin
                cnt  <= cnt + 1'b1;
            end
        end
    end
endmodule
