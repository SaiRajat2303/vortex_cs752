#!/bin/bash
# Run one regression app on rtlsim with the pipeline debug trace and keep only the
# events unit_activity.py needs (launch, warp, decode, dispatch, commit), without
# operand data. The full --debug=3 trace is dominated by cache traffic.
#
# Usage (from a configured build dir, env sourced):
#   CONFIGS="..." cs752_tools/trace_run.sh <app> <out.trace> [app args]
set -eu

app=$1
out=$2
args=${3:-}

fifo=$(mktemp -u "${out}.fifo.XXXX")
mkfifo "$fifo"
trap 'rm -f "$fifo"' EXIT

grep --line-buffered -E \
    ' (decode|dispatch|commit|warp-done|kmu-accept|start|cta-fire|dcr-write): |^PERF|PASSED|FAILED' \
    < "$fifo" \
  | sed -E 's/ (rs[123]_)?data=\{[^}]*\},?//g' > "$out" &
filter_pid=$!

make -C "tests/regression/$app" clean > /dev/null
make -C "tests/regression/$app" > /dev/null
rc=0
./ci/blackbox.sh --driver=rtlsim --app="$app" --perf=1 --debug=3 --log="$fifo" \
    ${args:+--args="$args"} > "${out}.stdout" 2>&1 || rc=$?
wait $filter_pid
exit $rc
