#!/usr/bin/env bash
# Workload-generator smoke test (no root needed). Run from code/:  ./tests/wlgen_smoke.sh
# Checks: calibration stability, same-seed schedule identity, EEVDF custom-slice support,
# CPU accounting under contention, and that every output file is produced.
set -uo pipefail
cd "$(dirname "$0")/.."
W=./build/wlgen
[[ -x $W ]] || { echo "build first: make"; exit 2; }
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
check() { if eval "$2"; then echo "PASS  $1"; else echo "FAIL  $1"; fail=1; fi; }

# 1. Calibration: three runs within 5% of each other. A larger spread is a WARN, not a FAIL: it
#    means the host moves this VM's vCPUs between fast and slow cores (hybrid CPUs) or is
#    throttling. Fix the host before a golden run (see docs/TESTING.md, "Host noise gate").
r=()
for i in 1 2 3; do r+=("$($W calibrate --trials 5 | python3 -c 'import json,sys; print(json.load(sys.stdin)["iters_per_us"])')"); done
IPU=${r[1]}
if python3 -c 'import sys; v=[float(x) for x in sys.argv[1:]]; sys.exit(0 if max(v)/min(v) < 1.05 else 1)' "${r[@]}"; then
	echo "PASS  calibration stable (${r[*]} iters/us)"
else
	echo "WARN  calibration spread > 5% (${r[*]} iters/us): host is moving vCPUs between core types or throttling"
fi

# 2. Contended run on one CPU: 2 batch threads (nice 10) + 2 interactive tasks (100 us slice)
CPU=$(( $(nproc) > 1 ? 1 : 0 ))
mkdir -p "$T/a" "$T/b"
S=$(python3 -c 'import time; print(time.monotonic_ns() + 300_000_000)')
$W batch --threads 2 --unit-us 1000 --seed 7 --start-at "$S" --warmup-s 1 --duration-s 3 \
	--out "$T/a" --cpus $CPU --iters-per-us "$IPU" --nice 10 &
$W interactive --tasks 2 --arrival poisson --rate 100 --work-us 1000 --seed 7 --start-at "$S" \
	--warmup-s 1 --duration-s 3 --out "$T/a" --cpus $CPU --iters-per-us "$IPU" --slice-us 100 &
wait
# 3. Same seed again, alone on a CPU
$W interactive --tasks 2 --arrival poisson --rate 100 --work-us 1000 --seed 7 --warmup-s 1 \
	--duration-s 3 --out "$T/b" --cpus $CPU --iters-per-us "$IPU"

for f in requests.csv interactive_summary.json batch.csv batch_ts.csv batch_summary.json; do
	check "output $f exists" "[[ -s $T/a/$f ]]"
done
check "same seed -> identical schedule hash" \
	"[[ \$(grep -h schedule_hash $T/a/interactive_summary.json) == \$(grep -h schedule_hash $T/b/interactive_summary.json) ]]"
check "kernel accepted the 100 us EEVDF slice (slice_readback_ns=100000)" \
	"grep -q '\"slice_readback_ns\": 100000' $T/a/interactive_summary.json"
check "batch threads run at nice 10" "grep -q '\"nice_readback\": 10' $T/a/batch_summary.json"

python3 - "$T" <<'EOF'
import csv, json, sys
T = sys.argv[1]
ok = True
b = list(csv.DictReader(open(f"{T}/a/batch.csv")))
batch_cpu = sum(int(r["cpu_ns"]) for r in b) / 3e9
work = sum(int(r["work_iters"]) for r in csv.DictReader(open(f"{T}/a/requests.csv")) if r["measured"] == "1")
ipu = json.load(open(f"{T}/a/interactive_summary.json"))["iters_per_us"]
inter_cpu = work / ipu / 1e6 / 3
total = batch_cpu + inter_cpu
print(f"{'PASS' if 0.9 < total < 1.05 else 'FAIL'}  one CPU fully used: batch {batch_cpu:.2f} + interactive {inter_cpu:.2f} = {total:.2f}")
ok &= 0.9 < total < 1.05
rt = sorted(int(r["t_end_ns"]) - int(r["t_arrival_ns"]) for r in csv.DictReader(open(f"{T}/a/requests.csv")) if r["measured"] == "1")
rt0 = sorted(int(r["t_end_ns"]) - int(r["t_arrival_ns"]) for r in csv.DictReader(open(f"{T}/b/requests.csv")) if r["measured"] == "1")
p99 = lambda x: x[int(0.99 * (len(x) - 1))] / 1e6
print(f"INFO  interactive p99: contended {p99(rt):.2f} ms vs alone {p99(rt0):.2f} ms")
units = [int(r["units"]) for r in b]
spread = max(units) / max(min(units), 1)
print(f"{'PASS' if spread < 1.2 else 'FAIL'}  batch threads progress evenly (units {units})")
ok &= spread < 1.2
sys.exit(0 if ok else 1)
EOF
[[ $? -eq 0 ]] || fail=1

echo
[[ $fail -eq 0 ]] && echo "wlgen_smoke: ALL PASS" || echo "wlgen_smoke: FAILURES ABOVE"
exit $fail
