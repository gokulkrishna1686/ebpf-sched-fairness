#!/usr/bin/env bash
# Regenerate every number, table and figure in the paper from the included raw data of the golden
# run. Needs no root and no eBPF, only python3 (numpy, pandas, scipy, matplotlib), zstd and tar;
# takes about 5 minutes. Run from code/:   ./tests/reproduce_paper.sh [timestamp]   (or: make reproduce)
#
#   1. check each archive's SHA-256 and unpack it under results/raw/
#   2. recompute metrics and statistics into results/reproduced/summary/
#   3. compare them with the committed results/summary/ files
#   4. redo the validation and rebuild the figures and tables in results/reproduced/
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
shopt -s nullglob

TS=${1:-20261106-174432}
RAW=results/raw
OUT=results/reproduced
BATCHES=(tuning main heldout ablation scaling overhead)
mkdir -p "$OUT/summary"
fail=0

echo "=== 1. checking and unpacking the raw data"
for b in "${BATCHES[@]}" validation; do
	name=${b}_$TS
	if [[ -d $RAW/$name ]]; then
		echo "$name: already unpacked"
		continue
	fi
	files=("$RAW/$name".tar.zst.part*)
	[[ ${#files[@]} -gt 0 ]] || files=("$RAW/$name".tar.zst)
	want=$(cut -d' ' -f1 "$RAW/$name.tar.zst.sha256")
	got=$(cat "${files[@]}" | sha256sum | cut -d' ' -f1)
	if [[ $want != "$got" ]]; then
		echo "FAIL  $name: checksum mismatch"
		exit 1
	fi
	if ! cat "${files[@]}" | zstd -dcq | tar -x -C "$RAW"; then
		echo "FAIL  $name: could not unpack"
		rm -rf "${RAW:?}/$name"
		exit 1
	fi
	echo "$name: checksum OK, unpacked"
done

echo
echo "=== 2. recomputing metrics and statistics"
for b in "${BATCHES[@]}"; do
	python3 -B analysis/metrics.py "$RAW/${b}_$TS" --out "$OUT/summary" &&
		python3 -B analysis/stats.py "$OUT/summary/${b}_${TS}_runs.csv" || fail=1
done

echo
echo "=== 3. comparing with the committed summaries"
python3 -B tests/compare_summaries.py "$OUT/summary" results/summary "$TS" || fail=1

echo
echo "=== 4. validation, figures and tables"
python3 -B analysis/validate.py --trace "$RAW/validation_$TS" --threads "$OUT/summary/main_${TS}_threads.csv" \
	--noise "$RAW/main_$TS" | tee "$OUT/summary/validation_$TS.txt" || fail=1
python3 -B analysis/plots.py --summary "$OUT/summary" --raw "$RAW" --out "$OUT/figures" --main "main_$TS" \
	--heldout "heldout_$TS" --ablation "ablation_$TS" --overhead "overhead_$TS" --scaling "scaling_$TS" || fail=1
python3 -B analysis/make_tables.py --summary "$OUT/summary" --raw "$RAW" --out "$OUT/tables" --main "main_$TS" \
	--heldout "heldout_$TS" --overhead "overhead_$TS" --ablation "ablation_$TS" || fail=1
python3 -B analysis/paper_numbers.py --summary "$OUT/summary" --ts "$TS" > "$OUT/paper_numbers.txt" || fail=1

echo
if [[ $fail -eq 0 ]]; then
	echo "REPRODUCE: PASS. Figures: $OUT/figures   Tables: $OUT/tables   Numbers in the text: $OUT/paper_numbers.txt"
else
	echo "REPRODUCE: FAILED, see the messages above"
fi
exit $fail
