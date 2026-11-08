// Frame receiver for: 0x5A, type, len, payload[len], CRC16 (big-endian, over type+len+payload).
// Payload bytes stream out as they arrive (pl_valid, pl_idx, pl_data). The caller must buffer them and
// ONLY act on them after frame_done with crc_ok = 1. A gap longer than TIMEOUT_CLKS between bytes
// abandons the frame and waits for the next sync byte.
//
// Header check (RX1, matches slowpath/frames.py F3/F4 and docs/FRAMES.md): once the length byte arrives,
// (type, len) must be a known type with its legal length. If not, the frame is rejected at once instead of
// swallowing `len` bytes (which used to hide up to 255 bytes of following good frames):
//   - frame_done pulses with crc_ok = 0 (so the caller counts it in frames_bad, like a bad CRC);
//   - the hunt restarts at the byte after the sync, like FrameDecoder: if the type byte was 0x5A it is
//     taken as the new sync (and the length byte as its type); else if the length byte is 0x5A it is
//     the new sync; else wait for the next 0x5A.
// RX2: a bad CRC (header was legal) still resyncs only after the end of that frame; hardware does not
//      buffer the frame to re-scan it the way frames.py does.
// CHECK_LEN = 0 disables the header check (old behaviour).
module frame_rx #(parameter int TIMEOUT_CLKS = 100_000,     // 2 ms at 50 MHz
                  parameter bit CHECK_LEN    = 1'b1) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [7:0] byte_in,
  input  logic       byte_valid,
  output logic       pl_valid,
  output logic [7:0] pl_idx,
  output logic [7:0] pl_data,
  output logic       frame_done,   // one-clock pulse at the end of every frame (also on a header reject)
  output logic       crc_ok,       // valid with frame_done; 0 on a bad CRC or header reject
  output logic [7:0] frame_type,
  output logic [7:0] frame_len
);
  typedef enum logic [2:0] {WAIT_SYNC, TYPE, LEN, PAYLOAD, CRC_HI, CRC_LO} state_t;
  state_t state;
  logic [15:0] crc, crc_next, crc_from_init;
  logic [15:0] rx_crc;
  logic [7:0]  idx;
  logic [$clog2(TIMEOUT_CLKS+1)-1:0] idle_cnt;

  crc16_ccitt_byte u_crc  (.crc_in(crc),      .d(byte_in), .crc_out(crc_next));
  crc16_ccitt_byte u_crc0 (.crc_in(16'hFFFF), .d(byte_in), .crc_out(crc_from_init));  // RX1 re-hunt

  // Legal payload length per type (frames.py F3). 0x13 load block: 2 + 2N, N = 1..126.
  function automatic logic len_ok(input logic [7:0] t, input logic [7:0] n);
    case (t)
      8'h10: len_ok = (n == 8'd95);
      8'h11: len_ok = (n == 8'd4);
      8'h12: len_ok = (n == 8'd10);
      8'h13: len_ok = (n[0] == 1'b0) && (n >= 8'd4) && (n <= 8'd254);
      8'h14: len_ok = (n == 8'd8);
      8'h15: len_ok = (n == 8'd0);
      8'h16: len_ok = (n == 8'd2);
      8'h17: len_ok = (n == 8'd16);
      8'h20: len_ok = (n == 8'd33);
      8'h21: len_ok = (n == 8'd20);
      default: len_ok = 1'b0;      // unknown type (F4)
    endcase
  endfunction

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= WAIT_SYNC; crc <= 16'hFFFF; rx_crc <= '0; idx <= '0; idle_cnt <= '0;
      pl_valid <= 1'b0; pl_idx <= '0; pl_data <= '0;
      frame_done <= 1'b0; crc_ok <= 1'b0; frame_type <= '0; frame_len <= '0;
    end else begin
      pl_valid <= 1'b0; frame_done <= 1'b0;
      if (byte_valid) idle_cnt <= '0;
      else if (state != WAIT_SYNC) begin
        if (idle_cnt == TIMEOUT_CLKS) begin state <= WAIT_SYNC; idle_cnt <= '0; end
        else idle_cnt <= idle_cnt + 1'b1;
      end
      if (byte_valid) begin
        case (state)
          WAIT_SYNC: if (byte_in == 8'h5A) begin crc <= 16'hFFFF; state <= TYPE; end
          TYPE: begin frame_type <= byte_in; crc <= crc_next; state <= LEN; end
          LEN: begin
            frame_len <= byte_in; idx <= '0;
            if (CHECK_LEN && !len_ok(frame_type, byte_in)) begin
              frame_done <= 1'b1; crc_ok <= 1'b0;                       // RX1 header reject
              if (frame_type == 8'h5A) begin                            // type byte was a sync:
                frame_type <= byte_in; crc <= crc_from_init; state <= LEN;  // this byte is its type
              end else if (byte_in == 8'h5A) begin
                crc <= 16'hFFFF; state <= TYPE;
              end else
                state <= WAIT_SYNC;
            end else begin
              crc <= crc_next;
              state <= (byte_in == 8'd0) ? CRC_HI : PAYLOAD;
            end
          end
          PAYLOAD: begin
            pl_valid <= 1'b1; pl_idx <= idx; pl_data <= byte_in; crc <= crc_next;
            idx <= idx + 1'b1;
            if (idx == frame_len - 1'b1) state <= CRC_HI;
          end
          CRC_HI: begin rx_crc[15:8] <= byte_in; state <= CRC_LO; end
          CRC_LO: begin
            crc_ok <= ({rx_crc[15:8], byte_in} == crc);
            frame_done <= 1'b1; state <= WAIT_SYNC;
          end
        endcase
      end
    end
  end
endmodule
