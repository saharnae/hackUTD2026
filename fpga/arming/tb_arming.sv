// tb_arming.sv - self-checking Questa testbench for arming.sv.
// Real 50 MHz clock and a real 1 us tick. The heartbeat timeout is the real 150,000 us;
// debounce and hold times are shortened (DEBOUNCE_US = 20, HOLD_US = 2000) for speed.
`timescale 1ns/1ps

module tb_arming;
    localparam int DEB  = 20;
    localparam int HOLD = 2000;
    localparam int HBT  = 150_000;
    localparam int HB_PERIOD_US = 1000;

    logic clk = 0, rst_n = 0, tick_1us = 0;
    logic arm_sw = 0, key0_n = 1, green_n = 1, kill_in = 0;
    logic hb_valid = 0, book_ready = 0, snapshot_ok = 0;
    logic [4:0] latch_req = '0;
    logic reject_pulse = 0;
    logic [3:0] reject_code = '0;

    logic armed, tripped, relay, buzzer_n, hb_ok, green_press;
    logic [3:0] latched_reason;
    logic [9:0] ledr;

    arming #(.DEBOUNCE_US(DEB), .HB_TIMEOUT_US(HBT), .HOLD_US(HOLD)) dut (.*);

    always #10 clk = ~clk;                                   // 50 MHz

    int tick_div = 0;
    always @(posedge clk) begin                              // 1 us tick, one cycle wide
        tick_div <= (tick_div == 49) ? 0 : tick_div + 1;
        tick_1us <= (tick_div == 49);
    end

    // ------------------------------------------------ heartbeat source
    bit hb_en = 1;
    longint us_now = 0, us_last_hb = 0;
    always @(posedge clk) if (tick_1us) us_now <= us_now + 1;
    initial forever begin
        repeat (HB_PERIOD_US) @(posedge clk iff tick_1us);
        if (hb_en) begin
            @(posedge clk) hb_valid <= 1;
            us_last_hb = us_now;
            @(posedge clk) hb_valid <= 0;
        end
    end

    // ------------------------------------------------ helpers
    int n_pass = 0, n_fail = 0;
    task automatic check(input bit cond, input string msg);
        if (cond) n_pass++;
        else begin
            n_fail++;
            $display("FAIL @%0t us=%0d: %s (armed=%0b tripped=%0b reason=%0d ledr=%b)",
                     $time, us_now, msg, armed, tripped, latched_reason, ledr);
        end
    endtask

    task automatic wait_us(input int n);
        repeat (n) @(posedge clk iff tick_1us);
        @(posedge clk);
    endtask

    task automatic wait_clk(input int n);
        repeat (n) @(posedge clk);
    endtask

    task automatic press_key0();
        key0_n = 0; wait_us(DEB + 5);
        key0_n = 1; wait_us(DEB + 5);
    endtask

    task automatic cycle_arm();
        arm_sw = 0; wait_us(5);
        arm_sw = 1; wait_us(5);
    endtask

    task automatic pulse_latch(input logic [4:0] bits);
        @(posedge clk) latch_req <= bits;
        @(posedge clk) latch_req <= '0;
        wait_clk(2);
    endtask

    task automatic clear_latch();                          // ARM low, KEY0, wait for 2 heartbeats
        arm_sw = 0; wait_us(5);
        press_key0();
        check(!tripped && latched_reason == 0 && !relay, "latch cleared with ARM low");
        wait_us(2 * HB_PERIOD_US + 100);
    endtask

    // ------------------------------------------------ test sequence
    int lat;
    initial begin
        // ---- Case 1: power on with ARM already up, every condition true
        arm_sw = 1; book_ready = 1; snapshot_ok = 1;
        wait_clk(5); rst_n = 1;
        wait_us(5 * HB_PERIOD_US);
        check(!armed, "C1 power-on with ARM up stays blocked");
        check(ledr[7] && ledr[6] && ledr[5] && !ledr[8], "C1 LEDs hb/book/snap on, armed off");
        cycle_arm();
        check(armed && ledr[8], "C1 armed after ARM cycled low->high");

        // ---- conditions must hold before the rising edge
        arm_sw = 0; book_ready = 0; wait_us(5);
        arm_sw = 1; wait_us(5);
        check(!armed, "C1b ARM rise with book not ready does not arm");
        book_ready = 1; wait_us(5);
        check(!armed, "C1b book becoming ready later does not arm without a new edge");
        cycle_arm();
        check(armed, "C1b armed after a fresh edge");

        // ---- soft reject LED hold (not a latch)
        @(posedge clk) begin reject_pulse <= 1; reject_code <= 4'd5; end
        @(posedge clk) reject_pulse <= 0;
        wait_clk(2);
        check(ledr[3:0] == 5 && armed && !tripped, "soft reject shows code 5, stays armed");
        wait_us(HOLD + 5);
        check(ledr[3:0] == 0, "soft reject LED clears after the hold time");

        // ---- Case 2a: kill pressed (short press) latches R15 immediately
        @(posedge clk) kill_in <= 1;
        lat = 0;
        while (!tripped && lat < 20) begin @(posedge clk); lat++; end
        check(tripped && lat <= 4, $sformatf("C2a kill latches within 4 cycles (took %0d)", lat));
        check(latched_reason == 15 && relay && !armed && !buzzer_n, "C2a R15, relay on, disarmed, buzzer on");
        check(ledr[3:0] == 15 && ledr[9] && !ledr[8], "C2a LEDR shows 15 and tripped");
        wait_us(50); kill_in = 0;
        wait_us(HOLD - 100);
        check(!buzzer_n, "C2a buzzer still on before hold time");
        wait_us(200);
        check(buzzer_n, "C2a buzzer off after hold time");
        check(tripped && relay && latched_reason == 15, "C2a latch and relay stay after kill released");
        clear_latch();
        cycle_arm();
        check(armed, "C2a re-armed after clear");

        // ---- Case 2b: kill wire unplugged (stays high)
        kill_in = 1; wait_clk(5);
        check(tripped && latched_reason == 15, "C2b unplugged kill latches R15");
        arm_sw = 0; wait_us(5);
        press_key0();
        check(tripped && latched_reason == 15, "C2b clear while still unplugged re-latches");
        kill_in = 0; wait_us(5);
        clear_latch();
        cycle_arm();
        check(armed, "C2b re-armed after plugging back in and clearing");

        // ---- Case 3: pull the heartbeat -> R14 after 150 ms
        @(posedge clk iff hb_valid);
        hb_en = 0;
        wait (us_now >= us_last_hb + HBT - 10);
        @(posedge clk);
        check(!tripped && hb_ok && armed, "C3 still armed 10 us before the timeout");
        wait (us_now >= us_last_hb + HBT + 10);
        @(posedge clk);
        check(tripped && latched_reason == 14 && !armed, "C3 R14 latched just after 150,000 us");
        check(!ledr[7] && ledr[3:0] == 14, "C3 LEDR7 heartbeat off, LEDR shows 14");

        // ---- Case 4: clearing with ARM up does nothing
        arm_sw = 1; wait_us(5);
        press_key0();
        check(tripped && latched_reason == 14, "C4 KEY0 with ARM up does not clear");
        hb_en = 1;
        clear_latch();
        cycle_arm();
        check(armed, "C4 re-armed after a proper clear");

        // ---- KEY0 glitch shorter than the debounce does not clear
        pulse_latch(5'b00100);                               // R13
        arm_sw = 0; wait_us(5);
        key0_n = 0; wait_us(DEB / 2); key0_n = 1; wait_us(DEB + 5);
        check(tripped && latched_reason == 13, "short KEY0 glitch is debounced away");
        clear_latch();
        cycle_arm();

        // ---- Case 5: two latches at once keep the first (lowest) cause
        pulse_latch(5'b00110);                               // R12 and R13 in the same cycle
        check(tripped && latched_reason == 12, "C5 R12+R13 same cycle -> 12");
        pulse_latch(5'b00001);                               // later R03
        @(posedge clk) kill_in <= 1; wait_clk(5); kill_in = 0;
        check(latched_reason == 12, "C5 later R03 and kill do not change the frozen cause");
        clear_latch();
        cycle_arm();
        pulse_latch(5'b00100);                               // R13 first
        pulse_latch(5'b00010);                               // then R12
        check(latched_reason == 13, "C5 R13 then R12 -> keeps 13");
        clear_latch();

        // ---- green button pulse (AR4)
        begin
            automatic int seen = 0;
            fork
                begin green_n = 0; wait_us(DEB + 5); green_n = 1; wait_us(DEB + 5); end
                forever @(posedge clk) if (green_press) seen++;
            join_any
            disable fork;
            check(seen == 1, "green button gives exactly one debounced press pulse");
        end

        $display("tb_arming: %0d passed, %0d failed", n_pass, n_fail);
        if (n_fail == 0) $display("TB PASS"); else $display("TB FAIL");
        $finish;
    end
endmodule
