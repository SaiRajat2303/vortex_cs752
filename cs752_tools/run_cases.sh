#!/bin/bash
# Trace and analyze every case in a case file.
# Usage (env sourced): cs752_tools/run_cases.sh <build_dir> <case_file> <out_dir>
# case_file lines: <id>|<app>|<CONFIGS>|<args>   ('#' starts a comment)
set -u
tools=$(cd "$(dirname "$0")" && pwd)
build=$1; cases=$2; out=$3
mkdir -p "$out"
cd "$build"
nm=$VORTEX_TOOLDIR/llvm-vortex/bin/llvm-nm
while IFS='|' read -r id app cfg args; do
  [[ -z "$id" || "$id" == \#* ]] && continue
  start=$(date +%s)
  CONFIGS="$cfg" "$tools/trace_run.sh" "$app" "$out/$id.trace" "$args"
  rc=$?
  python3 "$tools/unit_activity.py" "$out/$id.trace" --test "$id" \
      --elf "tests/regression/$app/kernel.elf" --nm "$nm" --out "$out/$id" \
      >> "$out/analyze.log" 2>&1
  echo "$id rc=$rc $(( $(date +%s) - start ))s" >> "$out/progress.log"
done < "$cases"
echo DONE >> "$out/progress.log"
