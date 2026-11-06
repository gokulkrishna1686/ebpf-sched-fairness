#!/usr/bin/env bash
# All automatic tests in one command, about 5 minutes. Run from code/:   sudo ./tests/run_all.sh
#
#   T2 workload generator smoke test      T3 collector selftest (eBPF loads, vs kernel schedstat)
#   T5 policy restore test x3             T6 quick end-to-end run + batch audit + analysis + figures
#   T4 trace validation (eBPF vs raw ftrace, wait by wait)
# Every test prints PASS/FAIL and the script keeps going after a failure. Next step if all pass:
# make pilot (about 1 hour), then make golden.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
[[ $EUID -eq 0 ]] || { echo "run_all.sh: run with sudo"; exit 2; }

TS=$(date +%Y%m%d-%H%M%S)
declare -A RESULT
ORDER=()

stage() {
	local name=$1
	shift
	echo
	echo "=== $name   ($(date +%H:%M:%S))"
	if "$@"; then RESULT[$name]=PASS; else RESULT[$name]=FAIL; fi
	ORDER+=("$name")
}

quick() {
	python3 harness/run_matrix.py harness/configs/quick.json --batch-id "quick_$TS" &&
		python3 tests/check_batch.py "results/raw/quick_$TS" &&
		python3 analysis/metrics.py "results/raw/quick_$TS" &&
		python3 analysis/stats.py "results/summary/quick_${TS}_runs.csv" &&
		python3 analysis/plots.py --main "quick_$TS" --out "results/quick_figures/quick_$TS" &&
		python3 analysis/validate.py --threads "results/summary/quick_${TS}_threads.csv" --noise "results/raw/quick_$TS"
}

trace() {
	python3 harness/validate_trace.py --out "results/raw/validation_$TS" &&
		python3 analysis/validate.py --trace "results/raw/validation_$TS"
}

make all || { echo "build failed"; exit 1; }
stage "T2 workload generator" ./tests/wlgen_smoke.sh
stage "T3 collector selftest" ./build/schedmon --selftest
for a in nice slice cgweight; do
	stage "T5 restore ($a)" python3 tests/restore_test.py --actuator "$a"
done
stage "T6 quick run + analysis" quick
stage "T4 trace validation" trace

# Everything ran as root: hand the repository back to the user.
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER": .. 2>/dev/null

echo
echo "=== TEST SUMMARY ($TS)"
fail=0
for s in "${ORDER[@]}"; do
	printf "%-4s  %s\n" "${RESULT[$s]}" "$s"
	[[ ${RESULT[$s]} == PASS ]] || fail=1
done
echo "quick figures: results/quick_figures/quick_$TS    trace check: results/raw/validation_$TS"
if [[ $fail -eq 0 ]]; then
	echo "ALL TESTS PASS -> next: make pilot (about 1 hour)"
else
	echo "SOME TESTS FAILED: scroll up to the === heading of the failed test and send that part"
fi
exit $fail
