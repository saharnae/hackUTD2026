# io_shock.sdc - one 50 MHz clock.
create_clock -name clk -period 20.000 [get_ports clk]
derive_clock_uncertainty
# rst_n comes from the arming reset synchronizer in the real top; standalone it is a pin.
set_false_path -from [get_ports rst_n]
