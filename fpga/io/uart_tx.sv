// UART transmitter, 8N1. CLKS_PER_BIT = clock frequency / baud (50 MHz / 1 Mbaud = 50).
// Handshake: when ready is 1, pulse valid for one clock with data stable. ready drops while sending.
module uart_tx #(parameter int CLKS_PER_BIT = 50) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic [7:0] data,
  input  logic       valid,
  output logic       ready,
  output logic       tx
);
  typedef enum logic [1:0] {IDLE, START, DATA, STOP} state_t;
  state_t state;
  logic [$clog2(CLKS_PER_BIT)-1:0] cnt;
  logic [2:0] bit_i;
  logic [7:0] sh;

  assign ready = (state == IDLE);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; tx <= 1'b1; cnt <= '0; bit_i <= '0; sh <= '0;
    end else begin
      case (state)
        IDLE: begin
          tx <= 1'b1;
          if (valid) begin sh <= data; cnt <= '0; state <= START; end
        end
        START: begin
          tx <= 1'b0;
          if (cnt == CLKS_PER_BIT-1) begin cnt <= '0; bit_i <= '0; state <= DATA; end
          else cnt <= cnt + 1'b1;
        end
        DATA: begin
          tx <= sh[bit_i];
          if (cnt == CLKS_PER_BIT-1) begin
            cnt <= '0;
            if (bit_i == 3'd7) state <= STOP; else bit_i <= bit_i + 1'b1;
          end else cnt <= cnt + 1'b1;
        end
        STOP: begin
          tx <= 1'b1;
          if (cnt == CLKS_PER_BIT-1) begin cnt <= '0; state <= IDLE; end
          else cnt <= cnt + 1'b1;
        end
      endcase
    end
  end
endmodule
