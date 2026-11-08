# Pin assignments for the DE10-Lite bring-up top level.
# Source in Quartus: Tools > Tcl Scripts, or Assignments > Import Assignments after copying to the .qsf.
# VERIFY each pin against the DE10-Lite User Manual pin table before programming.
set_location_assignment PIN_P11 -to MAX10_CLK1_50
set_location_assignment PIN_B8  -to KEY[0]
set_location_assignment PIN_A7  -to KEY[1]
set_location_assignment PIN_C10 -to SW[0]
set_location_assignment PIN_C11 -to SW[1]
set_location_assignment PIN_D12 -to SW[2]
set_location_assignment PIN_C12 -to SW[3]
set_location_assignment PIN_A12 -to SW[4]
set_location_assignment PIN_B12 -to SW[5]
set_location_assignment PIN_A13 -to SW[6]
set_location_assignment PIN_A14 -to SW[7]
set_location_assignment PIN_B14 -to SW[8]
set_location_assignment PIN_F15 -to SW[9]
set_location_assignment PIN_A8  -to LEDR[0]
set_location_assignment PIN_A9  -to LEDR[1]
set_location_assignment PIN_A10 -to LEDR[2]
set_location_assignment PIN_B10 -to LEDR[3]
set_location_assignment PIN_D13 -to LEDR[4]
set_location_assignment PIN_C13 -to LEDR[5]
set_location_assignment PIN_E14 -to LEDR[6]
set_location_assignment PIN_D14 -to LEDR[7]
set_location_assignment PIN_A11 -to LEDR[8]
set_location_assignment PIN_B11 -to LEDR[9]
set_location_assignment PIN_AB5 -to rx_in
set_location_assignment PIN_AB6 -to tx_o
set_location_assignment PIN_AB7 -to esp_tx_o
foreach p {MAX10_CLK1_50 KEY[0] KEY[1] SW[*] LEDR[*] rx_in tx_o esp_tx_o} {
  set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to $p
}
