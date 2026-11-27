# Power of a mapped netlist at another library corner (OpenSTA, vectorless
# unless SAIF is given). The netlist is mapped with the TT library; ASAP7 cell
# names are identical across corners, so only the Liberty file changes.
# Env: LIB, NETLIST, TOP, SDC, OUT (report file). Optional: SAIF, SAIF_INST.
#
# set_cmd_units must follow read_liberty: read_liberty resets the time unit to
# the library's (ps), and an SDC read after that takes the 1.25 ns clock period
# as 1.25 ps, inflating dynamic power ~1000x.
read_liberty $::env(LIB)
set_cmd_units -time ns -capacitance fF -voltage V -current mA -resistance kOhm -power mW -distance um
read_verilog $::env(NETLIST)
link_design $::env(TOP)
read_sdc $::env(SDC)
if {[info exists ::env(SAIF)] && $::env(SAIF) ne ""} {
  read_saif -scope $::env(SAIF_INST) $::env(SAIF)
}
report_power -digits 6 > $::env(OUT)
exit
