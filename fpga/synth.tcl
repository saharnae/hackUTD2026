# synth.tcl - standalone Quartus Prime Lite compile of one fpga/ block (map, fit, STA).
# Usage (from the block folder):  quartus_sh -t ../synth.tcl <top> <file.sv> [more .sv ...]
# Builds in ./quartus/ next to the sources. Device 10M50DAF484C7G, 50 MHz (<top>.sdc).
#
# Every port except clk and rst_n is a VIRTUAL_PIN. These blocks are not the board top:
# their ports connect to other logic inside the FPGA (io_shock alone has more ports than
# the package has pins), so the timing reported here is register-to-register.

load_package flow

set top  [lindex $argv 0]
set srcs [lrange $argv 1 end]
set here [pwd]
file mkdir quartus
cd quartus

project_new $top -overwrite
set_global_assignment -name FAMILY "MAX 10"
set_global_assignment -name DEVICE 10M50DAF484C7G
set_global_assignment -name TOP_LEVEL_ENTITY $top
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output
foreach f $srcs { set_global_assignment -name SYSTEMVERILOG_FILE [file join $here $f] }
set_global_assignment -name SDC_FILE [file join $here $top.sdc]
set_global_assignment -name NUM_PARALLEL_PROCESSORS 4

# Elaborate once to learn the port names, then make each one (except clk, rst_n) virtual.
# A "-to *" wildcard also catches clk and internal nodes, so ports are listed one by one.
execute_module -tool map
set n_virtual 0
foreach_in_collection id [get_names -filter * -node_type pin] {
    set name [get_name_info -info full_path $id]
    if {$name ne "clk" && $name ne "rst_n"} {
        set_instance_assignment -name VIRTUAL_PIN ON -to $name
        incr n_virtual
    }
}
post_message "virtual pins assigned: $n_virtual"
export_assignments

execute_flow -compile
project_close
