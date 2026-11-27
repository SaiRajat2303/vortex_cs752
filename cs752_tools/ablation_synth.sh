#!/bin/bash
# Per-domain area/power by ablation: synthesize the core (stock flat Yosys flow,
# `make timing`) with each power-gating domain removed, then report every
# netlist's power at the TT/SS/FF corners. Domain = full - variant.
#
# The stock flow inlines the hierarchy (read_slang) and this OpenSTA has no
# `report_power -hier`, so units cannot be split from one netlist; the gateable
# domains (TCU, TFU, FPU) are separate extensions, so removing one is clean.
#
# Usage (env sourced, RAM_FOLD_PYTHON set to a python with pyslang 11/12):
#   ablation_synth.sh <vortex build dir> <out dir> [vts] [variants]
set -u
tools=$(cd "$(dirname "$0")" && pwd)
build=$(cd "$1" && pwd); out=$2
vts=${3:-"lvt rvt"}
variants=${4:-"full notcu notfu nof"}
mkdir -p "$out"
yosys_dir=$build/hw/syn/yosys
libdir=$build/hw/syn/libs/asap7/lib
core_inc=$(cd "$build/.." && pwd)/hw/unittest/core

BASE="-DVX_CFG_NUM_WARPS=4 -DVX_CFG_NUM_THREADS=4"
TCU="-DVX_CFG_EXT_TCU_ENABLE -DVX_CFG_TCU_WGMMA_ENABLE -DVX_CFG_TCU_TF32_ENABLE"
TFU="-DVX_CFG_EXT_TFU_ENABLE"
cfg_of() {
  case $1 in
    full)  echo "$BASE $TCU $TFU" ;;
    notcu) echo "$BASE $TFU" ;;
    notfu) echo "$BASE $TCU" ;;
    # the TFU requires F (VX_define.vh), so no-F also drops the TFU: FPU = notfu - nof
    nof)   echo "$BASE $TCU -DVX_CFG_EXT_F_DISABLE" ;;
  esac
}

run_one() {
  local v=$1 vt=$2 pre=step3_${1}_${2}
  local dir=$yosys_dir/${pre}_VX_core_top
  if [[ ! -s $dir/reports/power.rpt ]]; then
    make -C "$yosys_dir" timing PREFIX=$pre ASAP7_VT=$vt TOP_LEVEL_ENTITY=VX_core_top \
      EXTRA_INCLUDE="-I$core_inc" EXTRA_CONFIGS="$(cfg_of $v)" > "$out/${pre}.log" 2>&1 \
      || { echo "$pre synth FAILED" >> "$out/progress.log"; return; }
  fi
  for c in tt ss ff; do
    LIB=$libdir/asap7_${vt}_${c}.lib NETLIST=$dir/out/VX_core_top_mapped.v TOP=VX_core_top \
    SDC=$dir/out/VX_core_top.resolved.sdc OUT=$out/${pre}_${c}_power.rpt \
      sta -no_init -exit "$tools/corner_power.tcl" > "$out/${pre}_${c}_sta.log" 2>&1
  done
  cp -f "$dir/reports/stat_lib.rpt" "$out/${pre}_stat_lib.rpt"
  cp -f "$dir/reports/worst_slack.rpt" "$out/${pre}_worst_slack.rpt"
  echo "$pre done" >> "$out/progress.log"
}

jobs=0
for vt in $vts; do
  for v in $variants; do
    run_one "$v" "$vt" &
    jobs=$((jobs + 1))
    if (( jobs % 3 == 0 )); then wait; fi
  done
done
wait
echo DONE >> "$out/progress.log"
