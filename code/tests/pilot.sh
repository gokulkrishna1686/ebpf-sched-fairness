#!/usr/bin/env bash
# Pilot (dress rehearsal) of the whole study, about 1 hour. Run from code/:   sudo ./tests/pilot.sh
#
# Every experiment config (tuning, main, heldout, ablation, scaling, overhead) runs with 4 s runs and
# its first 2 seeds, then trace validation and the complete analysis. That is the same pipeline,
# policies and code paths as the overnight golden run, only shorter. Every stage reports PASS/FAIL,
# and the script keeps going after a failure so one pilot shows every problem. Pilot data is for
# checking the pipeline only: figures carry a "PILOT - NOT RESULTS" watermark.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
[[ $EUID -eq 0 ]] || { echo "pilot.sh: run with sudo"; exit 2; }

TS=$(date +%Y%m%d-%H%M%S)
CONFIGS=(tuning main heldout ablation scaling overhead)
FIG=results/pilot_figures/$TS
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

make all || { echo "build failed"; exit 1; }
for c in "${CONFIGS[@]}"; do
	python3 harness/run_matrix.py "harness/configs/$c.json" --pilot --dry-run | head -1
done

for c in "${CONFIGS[@]}"; do
	stage "run $c" python3 harness/run_matrix.py "harness/configs/$c.json" --pilot --batch-id "pilot_${c}_$TS"
done
stage "validate-trace" python3 harness/validate_trace.py --seconds 2 --out "results/raw/pilot_validation_$TS"

for c in "${CONFIGS[@]}"; do
	stage "audit $c" python3 tests/check_batch.py "results/raw/pilot_${c}_$TS"
done
for c in "${CONFIGS[@]}"; do
	stage "analyze $c" bash -c "python3 analysis/metrics.py results/raw/pilot_${c}_$TS && \
		python3 analysis/stats.py results/summary/pilot_${c}_${TS}_runs.csv"
done
stage "tune.py" python3 analysis/tune.py "results/summary/pilot_tuning_${TS}_runs.csv"
stage "figures" python3 analysis/plots.py --main "pilot_main_$TS" --heldout "pilot_heldout_$TS" \
	--ablation "pilot_ablation_$TS" --overhead "pilot_overhead_$TS" --scaling "pilot_scaling_$TS" --out "$FIG"
stage "tables" python3 analysis/make_tables.py --main "pilot_main_$TS" --heldout "pilot_heldout_$TS" \
	--overhead "pilot_overhead_$TS" --ablation "pilot_ablation_$TS" --out "$FIG/tables"
stage "validation" python3 analysis/validate.py --trace "results/raw/pilot_validation_$TS" \
	--threads "results/summary/pilot_main_${TS}_threads.csv" --noise "results/raw/pilot_main_$TS"

# Everything here ran as root: give the whole code tree (results, build, __pycache__) back.
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER": .

echo
echo "=== PILOT SUMMARY ($TS)"
fail=0
for s in "${ORDER[@]}"; do
	printf "%-4s  %s\n" "${RESULT[$s]}" "$s"
	[[ ${RESULT[$s]} == PASS ]] || fail=1
done
echo "figures: $FIG    batches: results/raw/pilot_*_$TS"
if [[ $fail -eq 0 ]]; then
	echo "PILOT: ALL STAGES PASS -> ready for the golden run"
else
	echo "PILOT: FAILURES ABOVE -> fix them (and rerun the pilot) before the golden run"
fi
exit $fail
