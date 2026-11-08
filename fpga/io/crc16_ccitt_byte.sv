// One byte of CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF set by the caller). Combinational.
// Check value: the CRC of the ASCII bytes "123456789" starting from 0xFFFF is 0x29B1.
module crc16_ccitt_byte (
  input  logic [15:0] crc_in,
  input  logic [7:0]  d,
  output logic [15:0] crc_out
);
  logic [15:0] c;
  always_comb begin
    c = crc_in ^ {d, 8'h00};
    for (int i = 0; i < 8; i++)
      c = c[15] ? ((c << 1) ^ 16'h1021) : (c << 1);
    crc_out = c;
  end
endmodule
