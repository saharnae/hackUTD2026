# arming.sdc - one 50 MHz clock.
create_clock -name clk -period 20.000 [get_ports clk]
derive_clock_uncertainty
# rst_n is asynchronous; arming synchronizes its release internally (AR1).
set_false_path -from [get_ports rst_n]
