#!/usr/bin/env bash
# Golden run: the real study in one command, about 9 hours. Run from code/:
#
#   sudo ./tests/golden.sh              start a new golden run
#   sudo ./tests/golden.sh <TS>         resume the run with that timestamp (finished runs are kept)
#
# Order (the brief's tuning/evaluation split, automated):
#   1. tuning batch (seeds 1-3, mixes M1-M2 only)
#   2. analysis/tune.py --write freezes theta by the pre-declared rule, BEFORE any evaluation run
#   3. evaluation batches: main, heldout, ablation, scaling, overhead (seeds 101-105), packed for git
#   4. trace validation, batch audits, metrics/statistics
#   5. paper figures -> ../report/figures, tables + number macros -> ../report/tables
# Every stage reports PASS/FAIL; a failed stage does not stop the others. A log is written to
# results/summary/golden_<TS>.log. Keep the machine awake and plugged in for the whole run.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
[[ $EUID -eq 0 ]] || { echo "golden.sh: run with sudo"; exit 2; }

TS=${1:-$(date +%Y%m%d-%H%M%S)}
EVAL=(main heldout ablation scaling overhead)
mkdir -p results/raw results/summary
exec > >(tee -a "results/summary/golden_${TS}.log") 2>&1
declare -A RESULT
ORDER=()

stage() {
	local name=$1
	shift
	echo
	echo "=== $name   ($(date '+%a %H:%M:%S'))"
	if "$@"; then RESULT[$name]=PASS; else RESULT[$name]=FAIL; fi
	ORDER+=("$name")
}

run_batch() { # run, or resume if this batch already exists
	local c=$1 dir="results/raw/${1}_$TS"
	if [[ -d $dir ]]; then
		python3 harness/run_matrix.py "harness/configs/$c.json" --resume "$dir" --pack
	else
		python3 harness/run_matrix.py "harness/configs/$c.json" --batch-id "${c}_$TS" --pack
	fi
}

analyze() {
	python3 analysis/metrics.py "results/raw/${1}_$TS" && python3 analysis/stats.py "results/summary/${1}_${TS}_runs.csv"
}

freeze() { # only once per golden run: a resumed run keeps the parameters it froze
	if grep -q "tuning_$TS" harness/configs/params_frozen.json; then
		echo "parameters already frozen from tuning_$TS"
	else
		python3 analysis/tune.py "results/summary/tuning_${TS}_runs.csv" --write \
			| tee "results/summary/tuning_${TS}_choice.txt"
	fi
}

echo "golden run $TS on $(hostname), kernel $(uname -r)"
make all || { echo "build failed"; exit 1; }
for c in tuning "${EVAL[@]}"; do
	python3 harness/run_matrix.py "harness/configs/$c.json" --dry-run | head -1
done

stage "run tuning" run_batch tuning
stage "analyze tuning" analyze tuning
stage "freeze parameters" freeze
if [[ ${RESULT[freeze parameters]} != PASS ]]; then
	echo "parameters could not be frozen: stopping before evaluation"
	exit 1
fi
cp harness/configs/params_frozen.json "results/summary/params_frozen_${TS}.json"

for c in "${EVAL[@]}"; do
	stage "run $c" run_batch "$c"
done
stage "validate-trace" python3 harness/validate_trace.py --seconds 3 --out "results/raw/validation_$TS"

for c in tuning "${EVAL[@]}"; do
	stage "audit $c" python3 tests/check_batch.py "results/raw/${c}_$TS"
done
for c in "${EVAL[@]}"; do
	stage "analyze $c" analyze "$c"
done
stage "figures" python3 analysis/plots.py --main "main_$TS" --heldout "heldout_$TS" --ablation "ablation_$TS" \
	--overhead "overhead_$TS" --scaling "scaling_$TS"
stage "tables" python3 analysis/make_tables.py --main "main_$TS" --heldout "heldout_$TS" \
	--overhead "overhead_$TS" --ablation "ablation_$TS"
stage "validation" bash -c "python3 analysis/validate.py --trace results/raw/validation_$TS \
	--threads results/summary/main_${TS}_threads.csv --noise results/raw/main_$TS \
	| tee results/summary/validation_$TS.txt"

# Everything ran as root: hand the repository (results, report, params, build) back to the user.
[[ -n ${SUDO_USER:-} ]] && chown -R "$SUDO_USER": .. 2>/dev/null

echo
echo "=== GOLDEN RUN SUMMARY ($TS, finished $(date '+%a %H:%M'))"
fail=0
for s in "${ORDER[@]}"; do
	printf "%-4s  %s\n" "${RESULT[$s]}" "$s"
	[[ ${RESULT[$s]} == PASS ]] || fail=1
done
echo "batches: results/raw/*_$TS    figures: ../report/figures    tables: ../report/tables"
if [[ $fail -eq 0 ]]; then
	echo "GOLDEN RUN: ALL STAGES PASS"
else
	echo "GOLDEN RUN: SOME STAGES FAILED. Resume with: sudo ./tests/golden.sh $TS"
fi
exit $fail
