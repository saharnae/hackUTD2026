// Sends the 0x20 status frame from docs/FRAMES.md: 0x5A, 0x20, len 33, 33 payload bytes, CRC16 (38 bytes total).
// Fields are sampled together at the start of each frame, so one frame is always a consistent snapshot.
// PERIOD_MS = 100 is the FRAMES.md reset value (status period op 0x02). For a control-op period, drive period_ms_i.
module status_tx #(
  parameter int CLK_HZ    = 50_000_000,
  parameter int BAUD      = 1_000_000,
  parameter int PERIOD_MS = 100
) (
  input  logic        clk,
  input  logic        rst_n,
  input  logic        send_now,        // one-clock pulse: send a frame now (control op 0x01)
  input  logic [31:0] now_us,
  input  logic [7:0]  state,           // bit0 armed, 1 tripped, 2 book_ready, 3 snapshot_ok, 4 hb_ok, 5 shock_busy, 6 ARM switch, 7 kill pin
  input  logic [7:0]  trip_reason,
  input  logic [7:0]  last_reject,
  input  logic [7:0]  tokens,
  input  logic [15:0] snapshot_id,
  input  logic [15:0] ref_seq,
  input  logic [15:0] feed_seq,
  input  logic [20:0] s_cents_q4,      // S, u21, cents Q4
  input  logic [47:0] equity_x6400,    // s48
  input  logic [15:0] orders_sent,
  input  logic [15:0] fills,
  input  logic [19:0] last_latency_ns, // u20, 999_999 = saturated
  input  logic [7:0]  frames_bad,
  input  logic [7:0]  config_rejects,
  input  logic [7:0]  log_drops,
  output logic        tx
);
  localparam int CLKS_PER_BIT = CLK_HZ / BAUD;
  localparam int PERIOD_CLKS  = (CLK_HZ / 1000) * PERIOD_MS;
  localparam int PL_LEN       = 33;
  localparam int LAST_IDX     = 3 + PL_LEN + 1;     // 37: last CRC byte

  logic [7:0] tx_data; logic tx_valid, tx_ready;
  uart_tx #(.CLKS_PER_BIT(CLKS_PER_BIT)) u_tx (.clk, .rst_n, .data(tx_data), .valid(tx_valid), .ready(tx_ready), .tx);

  typedef enum logic [2:0] {S_WAIT, S_LOAD, S_HOLD, S_GAP, S_NEXT} state_t;
  state_t fsm;
  logic [$clog2(PERIOD_CLKS+1)-1:0] period_cnt;
  logic [7:0]  pl [0:PL_LEN-1];
  logic [5:0]  idx;                  // 0..37
  logic [15:0] crc, crc_next;
  logic [7:0]  cur_byte;
  logic        go;
  assign go = send_now | (period_cnt == PERIOD_CLKS-1);

  always_comb begin
    if      (idx == 6'd0)            cur_byte = 8'h5A;
    else if (idx == 6'd1)            cur_byte = 8'h20;
    else if (idx == 6'd2)            cur_byte = 8'(PL_LEN);
    else if (idx == 6'(3 + PL_LEN))  cur_byte = crc[15:8];
    else if (idx == 6'(4 + PL_LEN))  cur_byte = crc[7:0];
    else                             cur_byte = pl[idx - 6'd3];
  end

  crc16_ccitt_byte u_crc (.crc_in(crc), .d(cur_byte), .crc_out(crc_next));

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      fsm <= S_WAIT; period_cnt <= '0; idx <= '0; crc <= 16'hFFFF;
      tx_valid <= 1'b0; tx_data <= '0;
      for (int i = 0; i < PL_LEN; i++) pl[i] <= '0;
    end else begin
      tx_valid <= 1'b0;
      case (fsm)
        S_WAIT: begin
          if (go) begin
            period_cnt <= '0;
            pl[0]  <= now_us[31:24]; pl[1] <= now_us[23:16]; pl[2] <= now_us[15:8]; pl[3] <= now_us[7:0];
            pl[4]  <= state;  pl[5] <= trip_reason; pl[6] <= last_reject; pl[7] <= tokens;
            pl[8]  <= snapshot_id[15:8]; pl[9]  <= snapshot_id[7:0];
            pl[10] <= ref_seq[15:8];     pl[11] <= ref_seq[7:0];
            pl[12] <= feed_seq[15:8];    pl[13] <= feed_seq[7:0];
            pl[14] <= {3'b000, s_cents_q4[20:16]}; pl[15] <= s_cents_q4[15:8]; pl[16] <= s_cents_q4[7:0];
            pl[17] <= equity_x6400[47:40]; pl[18] <= equity_x6400[39:32]; pl[19] <= equity_x6400[31:24];
            pl[20] <= equity_x6400[23:16]; pl[21] <= equity_x6400[15:8];  pl[22] <= equity_x6400[7:0];
            pl[23] <= orders_sent[15:8]; pl[24] <= orders_sent[7:0];
            pl[25] <= fills[15:8];       pl[26] <= fills[7:0];
            pl[27] <= {4'b0000, last_latency_ns[19:16]}; pl[28] <= last_latency_ns[15:8]; pl[29] <= last_latency_ns[7:0];
            pl[30] <= frames_bad; pl[31] <= config_rejects; pl[32] <= log_drops;
            crc <= 16'hFFFF; idx <= '0; fsm <= S_LOAD;
          end else period_cnt <= period_cnt + 1'b1;
        end
        S_LOAD: if (tx_ready) begin
          tx_data <= cur_byte; tx_valid <= 1'b1;
          if (idx >= 6'd1 && idx <= 6'(2 + PL_LEN)) crc <= crc_next;   // type, len, payload only
          fsm <= S_HOLD;
        end
        S_HOLD: fsm <= S_GAP;      // valid was high for exactly one clock
        S_GAP:  fsm <= S_NEXT;     // let the transmitter leave IDLE
        S_NEXT: if (tx_ready) begin
          if (idx == 6'(LAST_IDX)) fsm <= S_WAIT;
          else begin idx <= idx + 1'b1; fsm <= S_LOAD; end
        end
        default: fsm <= S_WAIT;
      endcase
    end
  end
endmodule
