// UART receiver, 8N1, samples in the middle of each bit. valid is a one-clock pulse with data.
// frame_err pulses with valid if the stop bit was not 1 (the byte is still delivered; the caller may drop it).
module uart_rx #(parameter int CLKS_PER_BIT = 50) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       rx,
  output logic [7:0] data,
  output logic       valid,
  output logic       frame_err
);
  typedef enum logic [1:0] {IDLE, START, DATA, STOP} state_t;
  state_t state;
  logic r1, r2;                       // two-flop synchronizer for the asynchronous input
  logic [$clog2(CLKS_PER_BIT)-1:0] cnt;
  logic [2:0] bit_i;
  logic [7:0] sh;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      r1 <= 1'b1; r2 <= 1'b1; state <= IDLE; cnt <= '0; bit_i <= '0; sh <= '0;
      data <= '0; valid <= 1'b0; frame_err <= 1'b0;
    end else begin
      r1 <= rx; r2 <= r1;
      valid <= 1'b0; frame_err <= 1'b0;
      case (state)
        IDLE:  if (!r2) begin cnt <= '0; state <= START; end
        START: begin                  // wait half a bit, confirm the start bit is still low
          if (cnt == (CLKS_PER_BIT-1)/2) begin
            cnt <= '0;
            if (!r2) begin bit_i <= '0; state <= DATA; end else state <= IDLE;
          end else cnt <= cnt + 1'b1;
        end
        DATA: begin
          if (cnt == CLKS_PER_BIT-1) begin
            cnt <= '0; sh[bit_i] <= r2;
            if (bit_i == 3'd7) state <= STOP; else bit_i <= bit_i + 1'b1;
          end else cnt <= cnt + 1'b1;
        end
        STOP: begin
          if (cnt == CLKS_PER_BIT-1) begin
            cnt <= '0; data <= sh; valid <= 1'b1; frame_err <= !r2; state <= IDLE;
          end else cnt <= cnt + 1'b1;
        end
      endcase
    end
  end
endmodule
