// Bring-up top level for the DE10-Lite: proves the laptop <-> FPGA UART link at 1 Mbaud with the real 0x20 layout.
//   FPGA -> laptop: a 0x20 status frame (docs/FRAMES.md, 33-byte payload) on tx_o (Arduino IO1).
//   FPGA -> ESP32: the same frame, one way, 115200 baud, on esp_tx_o (Arduino IO2).
//   laptop -> FPGA: every byte received on rx_in (Arduino IO0) is counted into orders_sent (BRING-UP ONLY).
// Controls: KEY0 = reset, SW0 = armed, SW1 = tripped (trip_reason = 12), SW2 = hb_ok, SW4 = ARM switch.
//   shock_btn_n = shock button (active-low, held state), kill_btn_n = kill button (active-low, LATCHES until KEY0).
// Everything else in the frame is a fixed test value, so a reader can check each field.
// LEDs: LEDR0 = alive blink, LEDR1 = toggles on every received byte, LEDR2 = framing error seen,
//       LEDR3 = kill latched, LEDR4 = shock button held.
module latency_arena_top (
  input  logic       MAX10_CLK1_50,
  input  logic [1:0] KEY,
  input  logic [9:0] SW,
  output logic [9:0] LEDR,
  input  logic       rx_in,         // Arduino IO0 (adapter TXD goes here)
  output logic       tx_o,          // Arduino IO1 (adapter RXD goes here)
  output logic       esp_tx_o,      // Arduino IO2 (ESP32 RX2 / GPIO16 goes here), same frame at 115200 baud
  input  logic       kill_btn_n,    // kill button, active-low, weak pull-up (Arduino IO4, TODO(FINAL))
  input  logic       shock_btn_n    // shock button, active-low, weak pull-up (Arduino IO3, TODO(FINAL))
);
  logic clk, rst_n;
  assign clk   = MAX10_CLK1_50;
  assign rst_n = KEY[0];

  // Microsecond counter (50 clocks per us).
  logic [31:0] now_us; logic [5:0] us_div;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin now_us <= '0; us_div <= '0; end
    else if (us_div == 6'd49) begin us_div <= '0; now_us <= now_us + 1'b1; end
    else us_div <= us_div + 1'b1;

  // Buttons: 2-FF synchronizer + 2 ms debounce (active-low inputs).
  logic [1:0]  kill_sync, shock_sync;
  logic        kill_db, shock_db;
  logic [16:0] kill_cnt, shock_cnt;            // 99_999 clocks = 2 ms at 50 MHz
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      kill_sync <= 2'b11;  shock_sync <= 2'b11;
      kill_db   <= 1'b1;   shock_db   <= 1'b1;
      kill_cnt  <= '0;     shock_cnt  <= '0;
    end else begin
      kill_sync  <= {kill_sync[0],  kill_btn_n};
      shock_sync <= {shock_sync[0], shock_btn_n};
      if (kill_sync[1] == kill_db) kill_cnt <= '0;
      else if (kill_cnt == 17'd99_999) begin kill_db <= kill_sync[1]; kill_cnt <= '0; end
      else kill_cnt <= kill_cnt + 1'b1;
      if (shock_sync[1] == shock_db) shock_cnt <= '0;
      else if (shock_cnt == 17'd99_999) begin shock_db <= shock_sync[1]; shock_cnt <= '0; end
      else shock_cnt <= shock_cnt + 1'b1;
    end

  logic shock_pressed, kill_active;
  assign shock_pressed = ~shock_db;            // held state of the shock button
  always_ff @(posedge clk or negedge rst_n)    // kill LATCHES until KEY0 reset (it can only make things safer)
    if (!rst_n)        kill_active <= 1'b0;
    else if (!kill_db) kill_active <= 1'b1;

  // Receive path: count bytes.
  logic [7:0] rx_data; logic rx_valid, rx_ferr;
  uart_rx #(.CLKS_PER_BIT(50)) u_rx (.clk, .rst_n, .rx(rx_in), .data(rx_data), .valid(rx_valid), .frame_err(rx_ferr));

  logic [15:0] rx_count; logic rx_toggle, ferr_seen;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin rx_count <= '0; rx_toggle <= 1'b0; ferr_seen <= 1'b0; end
    else begin
      if (rx_valid) begin rx_count <= rx_count + 1'b1; rx_toggle <= ~rx_toggle; end
      if (rx_ferr) ferr_seen <= 1'b1;
    end
  end

  // Status fields (one set, shared by both transmitters).
  logic [7:0]  st_state;  logic [7:0] st_trip;
  // Bring-up only: bit 5 displays the shock button's held state.
  // It is not confirmation that the storm simulator is running.
  assign st_state = {
    kill_active, SW[4], shock_pressed, SW[2],
    1'b1, 1'b1, SW[1], (SW[0] & ~kill_active)
  };
  assign st_trip  = SW[1] ? 8'd12 : 8'd0;

  // Transmit path to the laptop: status frames at 1 Mbaud. 20 ms for a snappy bring-up (FRAMES.md reset value is 100 ms).
  status_tx #(.CLK_HZ(50_000_000), .BAUD(1_000_000), .PERIOD_MS(20)) u_status_pc (
    .clk, .rst_n, .send_now(1'b0), .now_us, .state(st_state), .trip_reason(st_trip),
    .last_reject(8'd0), .tokens(8'd20),
    .snapshot_id(16'h0102), .ref_seq(16'h0304), .feed_seq(16'h0506), .s_cents_q4(21'h123456),
    .equity_x6400(48'h000000001234), .orders_sent(rx_count), .fills(16'd7), .last_latency_ns(20'd600),
    .frames_bad(8'd0), .config_rejects(8'd0), .log_drops(8'd0), .tx(tx_o));

  // Transmit path to the ESP32: the same frame at 115200 baud, every 100 ms, one way. The ESP32 is display only.
  status_tx #(.CLK_HZ(50_000_000), .BAUD(115_200), .PERIOD_MS(100)) u_status_esp (
    .clk, .rst_n, .send_now(1'b0), .now_us, .state(st_state), .trip_reason(st_trip),
    .last_reject(8'd0), .tokens(8'd20),
    .snapshot_id(16'h0102), .ref_seq(16'h0304), .feed_seq(16'h0506), .s_cents_q4(21'h123456),
    .equity_x6400(48'h000000001234), .orders_sent(rx_count), .fills(16'd7), .last_latency_ns(20'd600),
    .frames_bad(8'd0), .config_rejects(8'd0), .log_drops(8'd0), .tx(esp_tx_o));

  // Alive blink (about 1.5 Hz).
  logic [24:0] blink;
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) blink <= '0; else blink <= blink + 1'b1;

  assign LEDR = {5'b0, shock_pressed, kill_active, ferr_seen, rx_toggle, blink[24]};
endmodule