// tb_io_shock.sv - self-checking Questa testbench for io_shock (SIMULATED data).
`timescale 1ns/1ps

module tb_io_shock;
    logic clk = 1'b0;
    always #10 clk = ~clk;                      // 50 MHz

    // shared 1 us counter (50 cycles per us)
    logic [31:0] now_us = 0;
    int          cyc_in_us = 0;
    always @(posedge clk) begin
        if (cyc_in_us == 49) begin cyc_in_us <= 0; now_us <= now_us + 1; end
        else cyc_in_us <= cyc_in_us + 1;
    end

    logic         rst_n = 1'b0;
    logic [8:0]   sw = '0;
    logic         green_press = 1'b0, armed = 1'b0, tripped = 1'b0;
    logic         load_we = 1'b0;
    logic [13:0]  load_addr = '0;
    logic [15:0]  load_data = '0;
    logic         ord_valid = 1'b0, ord_is_hedge = 1'b0, ord_side = 1'b0;
    logic [2:0]   ord_contract = '0;
    logic [15:0]  ord_qty = '0;
    logic [17:0]  ord_price = '0;
    logic         word_valid, t0_pulse, shock_busy, fill_overflow, sw_dir;
    logic [127:0] word;
    logic [7:0]   hex0, hex1, hex2, hex3, hex4, hex5, sw_size;
    logic [8:0]   dbg_jitter_q8;
    logic [7:0]   dbg_u [8];
    logic [24:0]  dbg_w_us;
    logic [24:0]  dbg_off_us [8];
    logic [19:0]  dbg_latency_ns;

    io_shock dut (.clk, .rst_n, .now_us, .sw, .green_press, .armed, .tripped,
                  .load_we, .load_addr, .load_data,
                  .ord_valid, .ord_is_hedge, .ord_contract, .ord_side, .ord_qty, .ord_price,
                  .word_valid, .word, .word_ready(1'b1),
                  .t0_pulse, .shock_busy, .fill_overflow,
                  .hex0, .hex1, .hex2, .hex3, .hex4, .hex5,
                  .sw_dir, .sw_size, .dbg_jitter_q8, .dbg_u, .dbg_w_us, .dbg_off_us, .dbg_latency_ns);

    // standalone LFSR and double dabble for the bulk tests
    logic       l_step = 1'b0, l_rst = 1'b1;
    logic [7:0] l_r;
    logic [8:0] l_jit;
    shock_lfsr u_l (.clk, .rst(l_rst), .step(l_step), .r(l_r), .jitter_q8(l_jit));

    logic        d_start = 1'b0, d_done;
    logic [19:0] d_bin = '0;
    logic [23:0] d_bcd;
    bin2bcd_dd u_d (.clk, .rst(l_rst), .start(d_start), .bin(d_bin), .bcd(d_bcd), .done(d_done));

    // ------------------------------------------------------------ bookkeeping
    int errors = 0, checks = 0;
    task automatic check(input bit ok, input string what);
        checks++;
        if (!ok) begin errors++; $display("FAIL: %s", what); end
    endtask

    typedef struct { logic [127:0] w; logic [31:0] t_us; } rx_t;
    rx_t rx [$];
    always @(posedge clk) if (word_valid) rx.push_back('{word, now_us});

    function automatic logic [7:0] crc8_ref(input logic [119:0] d);
        logic [7:0] c = 8'h00;
        for (int i = 119; i >= 0; i--) c = {c[6:0], 1'b0} ^ ((c[7] ^ d[i]) ? 8'h07 : 8'h00);
        return c;
    endfunction
    function automatic logic [7:0]  w_type (input logic [127:0] w); return w[127:120]; endfunction
    function automatic logic [15:0] w_seq  (input logic [127:0] w); return w[119:104]; endfunction
    function automatic logic [3:0]  w_id   (input logic [127:0] w); return w[71:68];   endfunction
    function automatic logic [17:0] w_bid  (input logic [127:0] w); return w[67:50];   endfunction
    function automatic logic [17:0] w_ask  (input logic [127:0] w); return w[49:32];   endfunction

    function automatic int seg_digit(input logic [7:0] s);   // -1 blank, -2 dash, -3 bad
        case (s)
            8'hC0: return 0; 8'hF9: return 1; 8'hA4: return 2; 8'hB0: return 3; 8'h99: return 4;
            8'h92: return 5; 8'h82: return 6; 8'hF8: return 7; 8'h80: return 8; 8'h90: return 9;
            8'hFF: return -1; 8'hBF: return -2;
            default: return -3;
        endcase
    endfunction
    function automatic int hex_value();                      // -2 = all dashes, -3 = bad
        logic [7:0] s [6] = '{hex0, hex1, hex2, hex3, hex4, hex5};
        int v = 0, dashes = 0, d;
        for (int i = 5; i >= 0; i--) begin
            d = seg_digit(s[i]);
            if (d == -2) dashes++;
            else if (d == -3) return -3;
            else if (d >= 0) v = v * 10 + d;
        end
        return (dashes == 6) ? -2 : v;
    endfunction

    task automatic tick(input int n = 1); repeat (n) @(posedge clk); endtask
    task automatic wait_us(input int n); tick(n * 50); endtask
    task automatic press(); green_press <= 1'b1; tick(); green_press <= 1'b0; endtask
    task automatic load(input logic [13:0] a, input logic [15:0] d);
        load_we <= 1'b1; load_addr <= a; load_data <= d; tick(); load_we <= 1'b0;
    endtask
    function automatic logic [13:0] taddr(input bit dir, input logic [7:0] size, input int c, input bit is_ask);
        return {1'b0, dir, size, 3'(c), is_ask};
    endfunction
    function automatic int tbid(input bit dir, input int size, input int c);
        return 1000 + 100 * c + (dir ? 3 * size : -2 * size);
    endfunction
    task automatic load_rows(input bit dir, input int size);
        for (int c = 0; c < 8; c++) begin
            load(taddr(dir, 8'(size), c, 1'b0), 16'(tbid(dir, size, c)));
            load(taddr(dir, 8'(size), c, 1'b1), 16'(tbid(dir, size, c) + 2));
        end
    endtask
    task automatic send_order(input int c, input bit side, input int qty, input int price);
        ord_valid <= 1'b1; ord_contract <= 3'(c); ord_side <= side; ord_qty <= 16'(qty);
        ord_price <= 18'(price); ord_is_hedge <= 1'b0;
        tick();
        ord_valid <= 1'b0;
    endtask

    // ------------------------------------------------------------ run one shock and check it
    task automatic run_shock(input bit dir, input int size, input bit with_orders, input int lat_cycles,
                             input int med_us, input int slider_q8);
        int n0 = rx.size();
        logic [31:0] t_press, t0;
        int off [8];
        int c_late = 0, c_early = 0, w, n_ref;
        logic [15:0] seq0;
        sw = {dir, 8'(size)};
        tick(4);
        t_press = now_us;
        press();
        // base quotes
        wait (rx.size() >= n0 + 9);
        tick(2);
        seq0 = w_seq(rx[n0].w);
        check(w_type(rx[n0].w) == 1 && w_id(rx[n0].w) == 8 && w_bid(rx[n0].w) == 14713 && w_ask(rx[n0].w) == 14714,
              "base underlying quote");
        for (int c = 0; c < 8; c++) begin
            rx_t r = rx[n0 + 1 + c];
            check(w_type(r.w) == 2 && w_id(r.w) == c && w_bid(r.w) == tbid(dir, 0, c) && w_ask(r.w) == tbid(dir, 0, c) + 2,
                  $sformatf("base option quote c=%0d", c));
        end
        // W and offsets (IO5)
        check(dbg_jitter_q8 >= 129 && dbg_jitter_q8 <= 383, "jitter in 0.5..1.5");
        w = int'((longint'(med_us) * slider_q8 * int'(dbg_jitter_q8)) >>> 16);
        check(dbg_w_us == w, $sformatf("W = %0d us (expect %0d)", dbg_w_us, w));
        for (int c = 0; c < 8; c++) begin
            longint fac = 65536 - 51 * longint'(dbg_u[c]);
            off[c] = int'((longint'(w) * fac) >>> 16);
            check(dbg_off_us[c] == off[c], $sformatf("off[%0d] = %0d (expect %0d)", c, dbg_off_us[c], off[c]));
            check(off[c] * 1000 >= w * 799 && off[c] <= w, $sformatf("off[%0d] within W*(0.8..1)", c));
            if (off[c] > off[c_late]) c_late = c;
            if (off[c] < off[c_early]) c_early = c;
        end
        // shocked underlying, 1 ms after the press = T0
        @(posedge clk iff t0_pulse);
        t0 = now_us;
        check(t0 - t_press >= 1000 && t0 - t_press <= 1002, $sformatf("T0 %0d us after press", t0 - t_press));
        if (with_orders) begin
            tick(lat_cycles - 1);
            send_order(c_late, 1'b0, 3, 777);                     // before its refresh: fills
            press();                                              // busy: must be ignored
        end
        tick(2);
        begin
            rx_t r = rx[n0 + 9];
            check(w_type(r.w) == 1 && w_bid(r.w) == (dir ? 14713 + size : 14713 - size)
                  && w_ask(r.w) == (dir ? 14714 + size : 14714 - size), "shocked underlying quote");
        end
        if (with_orders) begin
            wait (now_us - t0 > off[c_early] + 2);
            tick();
            send_order(c_early, 1'b1, 4, 555);                    // after its refresh: qty 0
        end
        wait (!shock_busy);
        tick(100);
        // refresh words and fills
        n_ref = 0;
        for (int i = n0 + 10; i < rx.size(); i++) begin
            rx_t r = rx[i];
            if (w_type(r.w) == 2) begin
                int c = w_id(r.w);
                n_ref++;
                check(w_bid(r.w) == tbid(dir, size, c) && w_ask(r.w) == tbid(dir, size, c) + 2,
                      $sformatf("refresh quote c=%0d", c));
                check(r.t_us - t0 >= off[c] && r.t_us - t0 <= off[c] + 1,
                      $sformatf("refresh c=%0d at +%0d us (off %0d)", c, r.t_us - t0, off[c]));
            end else if (w_type(r.w) == 3) begin
                check(with_orders && w_id(r.w) == c_late && w_bid(r.w) == 777 && w_ask(r.w) == 3, "early order fills in full");
            end else if (w_type(r.w) == 4) begin
                check(with_orders && w_id(r.w) == c_early && w_bid(r.w) == 555 && w_ask(r.w) == 0, "late order fill qty 0");
            end else begin
                check(0, $sformatf("unexpected word type %0d", w_type(r.w)));
            end
        end
        check(n_ref == 8, $sformatf("8 refresh words (got %0d)", n_ref));
        check(rx.size() == n0 + 18 + (with_orders ? 2 : 0), $sformatf("word count %0d (second press ignored)", rx.size() - n0));
        for (int i = n0; i < rx.size(); i++)
            check(w_seq(rx[i].w) == 16'(seq0 + (i - n0)), "seq increments by 1");
        tick(40);
        if (with_orders)
            check(hex_value() == lat_cycles * 20, $sformatf("HEX latency %0d ns (expect %0d)", hex_value(), lat_cycles * 20));
        else
            check(hex_value() == -2, "HEX dashes when the shock gets no order");
    endtask

    // ------------------------------------------------------------ main
    initial begin
        tick(5);
        rst_n <= 1'b1;
        l_rst <= 1'b0;
        tick(5);

        // CRC-8 reference function against golden.build_word values
        check(crc8_ref(120'h0100000000000080e5e4397a064064) == 8'h55, "crc8 golden word 1");
        check(crc8_ref(120'h020005000004d230032000ca00a00a) == 8'h45, "crc8 golden word 2");
        check(crc8_ref(120'h03ffff000f423f7003e80004000000) == 8'h7d, "crc8 golden word 3");

        // 1. switch decode
        sw = 9'b1_0000_1010;
        tick(3);
        check(sw_dir == 1'b1 && sw_size == 8'd10, "SW=0b100001010 decodes as up, 10 cents");

        // 2. shock of 0 does nothing
        armed = 1'b1;
        sw = 9'b1_0000_0000;
        tick(3);
        press();
        wait_us(2000);
        check(rx.size() == 0 && !shock_busy, "shock size 0 does nothing");

        // 3. not armed / tripped: nothing
        sw = 9'b1_0000_1010;
        armed = 1'b0; tick(3); press(); wait_us(50);
        check(rx.size() == 0 && !shock_busy, "no shock when not armed");
        armed = 1'b1; tripped = 1'b1; tick(3); press(); wait_us(50);
        check(rx.size() == 0 && !shock_busy, "no shock when tripped");
        tripped = 1'b0;
        check(hex_value() == -2, "HEX dashes before any measurement");

        // 4. full shock, up 10 cents, with orders (latency 37 cycles = 740 ns)
        load_rows(1'b1, 0);
        load_rows(1'b1, 10);
        run_shock(1'b1, 10, 1'b1, 37, 100, 256);

        // 5. down 5 cents, no orders, slower market maker (median 150 us, slider 0.75)
        load_rows(1'b0, 0);
        load_rows(1'b0, 5);
        load(14'h2008, 16'd150);
        load(14'h2009, 16'd192);
        run_shock(1'b0, 5, 1'b0, 0, 150, 192);

        // 6. 10,000 LFSR draws: jitter stays in 0.5..1.5, full period 255
        begin
            automatic bit seen [256];
            automatic int distinct = 0, jmin = 999, jmax = 0;
            for (int i = 0; i < 10_000; i++) begin
                l_step <= 1'b1; tick(); l_step <= 1'b0; #1;
                check(l_jit >= 128 && l_jit <= 384, $sformatf("draw %0d jitter %0d/256 in 0.5..1.5", i, l_jit));
                check(l_r != 0, "LFSR never 0");
                if (!seen[l_r]) begin seen[l_r] = 1; distinct++; end
                if (l_jit < jmin) jmin = l_jit;
                if (l_jit > jmax) jmax = l_jit;
            end
            check(distinct == 255, $sformatf("LFSR visits all 255 states (%0d)", distinct));
            $display("LFSR jitter range over 10,000 draws: %0d/256 .. %0d/256", jmin, jmax);
        end

        // 7. double dabble
        begin
            automatic int vals [5] = '{0, 7, 740, 123456, 999999};
            foreach (vals[i]) begin
                automatic int got = 0;
                d_bin <= 20'(vals[i]); d_start <= 1'b1; tick(); d_start <= 1'b0;
                @(posedge clk iff d_done); #1;
                for (int k = 5; k >= 0; k--) got = got * 10 + d_bcd[k*4 +: 4];
                check(got == vals[i], $sformatf("double dabble %0d -> %0d", vals[i], got));
            end
        end

        // every emitted word carries a correct CRC-8
        foreach (rx[i]) check(crc8_ref(rx[i].w[127:8]) == rx[i].w[7:0], $sformatf("word %0d CRC-8", i));
        check(!fill_overflow, "no fill FIFO overflow");

        $display("tb_io_shock: %0d checks, %0d errors -> %s", checks, errors, errors == 0 ? "PASS" : "FAIL");
        $finish;
    end

    initial begin
        #200ms;
        $display("tb_io_shock: TIMEOUT");
        $finish;
    end
endmodule
