#!/bin/bash
# Run cases on rtlsim with core perf counters; writes <out>/summary.txt (pass, cycles) and
# <out>/cg.txt (the CG: lines of VX_unit_cg). Usage (any shell): regress_cg.sh <build> <case_file> <out>
set -u
: "${VORTEX_TOOLDIR:?source the environment script first}"
BUILD=$1; CASES=$2; OUT=$3
mkdir -p "$OUT"; cd "$BUILD"
while IFS='|' read -r id app cfg args; do
  [[ -z "$id" || "$id" == \#* ]] && continue
  log="$OUT/$id.log"
  make -C tests/regression/$app clean > "$log" 2>&1
  CONFIGS="$cfg" make -C tests/regression/$app >> "$log" 2>&1
  CONFIGS="$cfg" ./ci/blackbox.sh --driver=rtlsim --app=$app --perf=1 ${args:+--args="$args"} >> "$log" 2>&1
  rc=$?
  res=$(grep -qE "^PASSED" "$log" && echo PASS || echo FAIL)
  cyc=$(grep -E "^PERF: instrs=" "$log" | tail -1)
  echo "$id|rc=$rc|$res|$cyc" >> "$OUT/summary.txt"
  grep -E "^CG:" "$log" | sed "s/^/$id|/" >> "$OUT/cg.txt"
done < "$CASES"
echo DONE >> "$OUT/summary.txt"
