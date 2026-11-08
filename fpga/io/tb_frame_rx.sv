// Self-checking testbench for frame_rx (RX1 header reject / resync, RX2, timeout).
// Run (Questa FSE):  vlog -sv crc16_ccitt_byte.sv frame_rx.sv tb_frame_rx.sv ; vsim -c tb_frame_rx -do "run -all; quit -f"
`timescale 1ns/1ps
module tb_frame_rx;
  logic clk = 0, rst_n = 0;
  always #10 clk = ~clk;                         // 50 MHz

  logic [7:0] byte_in; logic byte_valid;
  logic pl_valid, frame_done, crc_ok; logic [7:0] pl_idx, pl_data, frame_type, frame_len;
  logic pl_valid_o, frame_done_o, crc_ok_o; logic [7:0] pl_idx_o, pl_data_o, frame_type_o, frame_len_o;

  frame_rx #(.TIMEOUT_CLKS(200)) dut (.clk, .rst_n, .byte_in, .byte_valid, .pl_valid, .pl_idx, .pl_data,
                                      .frame_done, .crc_ok, .frame_type, .frame_len);
  // Same stream into the old behaviour, to show what the check buys.
  frame_rx #(.TIMEOUT_CLKS(200), .CHECK_LEN(1'b0)) old (.clk, .rst_n, .byte_in, .byte_valid,
      .pl_valid(pl_valid_o), .pl_idx(pl_idx_o), .pl_data(pl_data_o), .frame_done(frame_done_o),
      .crc_ok(crc_ok_o), .frame_type(frame_type_o), .frame_len(frame_len_o));

  // Scoreboard: every frame_done is logged.
  int n_good, n_bad, n_good_old, errors;
  logic [7:0] good_types[$];
  logic [7:0] pl_buf[256];
  logic [7:0] last_pl[$];
  always @(posedge clk) begin
    if (pl_valid) pl_buf[pl_idx] = pl_data;
    if (frame_done) begin
      if (crc_ok) begin
        n_good++; good_types.push_back(frame_type);
        last_pl.delete(); for (int i = 0; i < frame_len; i++) last_pl.push_back(pl_buf[i]);
      end else n_bad++;
    end
    if (frame_done_o && crc_ok_o) n_good_old++;
  end

  function automatic logic [15:0] crc16(input logic [7:0] b[$]);
    logic [15:0] c = 16'hFFFF;
    foreach (b[k]) begin
      c ^= {b[k], 8'h00};
      for (int i = 0; i < 8; i++) c = c[15] ? ((c << 1) ^ 16'h1021) : (c << 1);
    end
    return c;
  endfunction

  typedef logic [7:0] bq_t[$];
  function automatic bq_t frame(input logic [7:0] t, input logic [7:0] pl[$]);
    bq_t body, f; logic [15:0] c;
    body.push_back(t); body.push_back(pl.size());
    foreach (pl[i]) body.push_back(pl[i]);
    c = crc16(body);
    f.push_back(8'h5A); foreach (body[i]) f.push_back(body[i]);
    f.push_back(c[15:8]); f.push_back(c[7:0]);
    return f;
  endfunction

  task automatic send(input bq_t s);               // back-to-back bytes, 3 idle clocks apart
    foreach (s[i]) begin
      @(negedge clk); byte_in = s[i]; byte_valid = 1;
      @(negedge clk); byte_valid = 0;
      repeat (2) @(negedge clk);
    end
    repeat (4) @(negedge clk);
  endtask

  task automatic reset_sb();
    n_good = 0; n_bad = 0; n_good_old = 0; good_types.delete();
  endtask

  task automatic check(input string name, input int exp_good, input int exp_bad, input bq_t exp_types);
    bit ok = (n_good == exp_good) && (n_bad == exp_bad) && (good_types.size() == exp_types.size());
    if (ok) foreach (exp_types[i]) if (good_types[i] != exp_types[i]) ok = 0;
    $display("%s %-44s good=%0d bad=%0d (expected %0d/%0d)  old-behaviour good=%0d",
             ok ? "PASS" : "FAIL", name, n_good, n_bad, exp_good, exp_bad, n_good_old);
    if (!ok) errors++;
    reset_sb();
  endtask

  bq_t hb, kill, ctl, s, bad, cfg;
  initial begin
    byte_in = 0; byte_valid = 0; errors = 0; reset_sb();
    repeat (3) @(negedge clk); rst_n = 1; repeat (2) @(negedge clk);

    hb   = frame(8'h11, '{8'h01, 8'h02, 8'h00, 8'h2A});
    kill = frame(8'h15, '{});
    ctl  = frame(8'h16, '{8'h01, 8'h00});

    // 1. Baseline.
    send(hb);
    check("good heartbeat", 1, 0, '{8'h11});
    if (last_pl != '{8'h01, 8'h02, 8'h00, 8'h2A}) begin $display("FAIL heartbeat payload"); errors++; end

    // 2. Heartbeat header claiming 200 bytes, then two good frames with no gap (the bug case).
    s = '{8'h5A, 8'h11, 8'd200, 8'h01, 8'h02}; s = {s, kill, ctl};
    send(s);
    check("bad length (200) then kill + control", 2, 1, '{8'h15, 8'h16});

    // 3. Length 255 on the snapshot type, then a good heartbeat.
    s = '{8'h5A, 8'h10, 8'hFF}; s = {s, hb};
    send(s);
    check("bad length (255, snapshot) then heartbeat", 1, 1, '{8'h11});

    // 4. Unknown type, then a good frame.
    s = '{8'h5A, 8'h33, 8'h04, 8'hAA}; s = {s, ctl};
    send(s);
    check("unknown type then control", 1, 1, '{8'h16});

    // 5. Odd load-block length (illegal), then a good frame.
    s = '{8'h5A, 8'h13, 8'd5}; s = {s, kill};
    send(s);
    check("odd load-block length then kill", 1, 1, '{8'h15});

    // 6. Stray 0x5A right before a real frame: 5A 5A 11 04 ... (type byte is the real sync).
    s = '{8'h5A}; s = {s, hb};
    send(s);
    check("stray sync before heartbeat", 1, 1, '{8'h11});

    // 7. Real frame starts in the length position: 5A 11 [5A 15 00 crc crc].
    s = '{8'h5A, 8'h11}; s = {s, kill};
    send(s);
    check("real sync in length byte", 1, 1, '{8'h15});

    // 8. Bad CRC on a legal header, then a good frame (RX2: resync after the frame).
    bad = frame(8'h16, '{8'h02, 8'h05}); bad[bad.size()-1] ^= 8'hFF; s = {bad, hb};
    send(s);
    check("bad CRC then heartbeat", 1, 1, '{8'h11});

    // 9. Truncated legal frame, gap > timeout, then a good frame.
    s = '{8'h5A, 8'h11, 8'h04, 8'h01};
    send(s); repeat (300) @(negedge clk);
    send(hb);
    check("truncated frame, timeout, heartbeat", 1, 0, '{8'h11});

    // 10. Max-length legal load block still works (254 bytes).
    begin
      bq_t pl; for (int i = 0; i < 254; i++) pl.push_back(i[7:0]);
      send(frame(8'h13, pl));
      check("254-byte load block", 1, 0, '{8'h13});
    end

    // 11. Garbage burst (no 0x5A) between good frames.
    s = {kill, bq_t'('{8'h00, 8'hFF, 8'h11, 8'h04, 8'h33}), ctl};
    send(s);
    check("garbage between frames", 2, 0, '{8'h15, 8'h16});

    if (errors == 0) $display("ALL TESTS PASSED");
    else             $display("%0d TEST(S) FAILED", errors);
    $finish;
  end
endmodule
