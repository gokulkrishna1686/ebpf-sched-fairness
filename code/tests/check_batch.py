#!/usr/bin/env python3
"""Audit a finished batch before any analysis. Prints PASS/WARN/FAIL; exit 1 on any FAIL.

  python3 tests/check_batch.py results/raw/<batch_id>

Checks:
  every run         status ok, restore_check ok, schedmon exit 0
  collector         no dropped events, no actuator or restore errors
  workload          no thread setup errors, first requests not late
  baselines         B1 batch threads really at nice 10, B3 interactive slice really applied
  pairing           identical schedule_hash for every policy with the same (mix, cores, seed)
  bookkeeping       failed_runs.csv listed, noise-floor run present
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
import baselines  # noqa: E402

counts = defaultdict(int)


def report(level, msg):
    counts[level] += 1
    print(f"{level:4s}  {msg}")


def load(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    batch = Path(sys.argv[1])
    cfg = load(batch / "config.json") or {}
    baselines.register(cfg.get("policies_custom"))
    runs = sorted(p.parent for p in batch.glob("*/meta.json") if p.parent.name != "noise_floor")
    if not runs:
        sys.exit(f"no runs under {batch}")
    print(f"batch {batch.name}: {len(runs)} run directories")

    hashes = defaultdict(set)
    for d in runs:
        meta = load(d / "meta.json") or {}
        rid, c = d.name, meta.get("config", {})
        if meta.get("status") != "ok":
            report("FAIL", f"{rid}: status {meta.get('status')}")
            continue
        if not (meta.get("restore_check") or {}).get("ok"):
            report("FAIL", f"{rid}: restore_check {meta.get('restore_check')}")
        if meta.get("schedmon_rc") not in (None, 0):
            report("FAIL", f"{rid}: schedmon exit code {meta.get('schedmon_rc')}")

        st = load(d / "schedmon_stats.json")
        if st:
            em, dr = st.get("events_emitted", 0), st.get("events_dropped", 0)
            if dr:
                report("FAIL" if dr > 0.001 * max(em + dr, 1) else "WARN", f"{rid}: {dr} dropped events of {em + dr}")
            for k in ("actuator_errors", "restore_errors"):
                if st.get(k):
                    report("FAIL", f"{rid}: {k} = {st[k]}")

        pol = baselines.resolve(c.get("policy", "default"))
        for role in ("interactive", "batch"):
            s = load(d / f"{role}_summary.json")
            if not s:
                continue
            for th in s.get("threads", []):
                if th.get("setup_errno"):
                    report("FAIL", f"{rid}: {role} thread {th['tid']} setup errno {th['setup_errno']}")
            if role == "interactive":
                hashes[(c.get("mix"), c.get("cores"), c.get("seed"))].add(s.get("schedule_hash"))
                if s.get("late_first_request_tasks"):
                    report("WARN", f"{rid}: {s['late_first_request_tasks']} task(s) started late (>10 ms after t0)")
                want = pol.get("interactive_slice_us")
                if want and any(th.get("slice_readback_ns") != want * 1000 for th in s["threads"]):
                    report("FAIL", f"{rid}: B3 slice not applied: {[th.get('slice_readback_ns') for th in s['threads']]}")
            if role == "batch" and "batch_nice" in pol:
                if any(th.get("nice_readback") != pol["batch_nice"] for th in s["threads"]):
                    report("FAIL", f"{rid}: batch nice not applied: {[th.get('nice_readback') for th in s['threads']]}")

    for key, hs in hashes.items():
        if len(hs) > 1:
            report("FAIL", f"schedule differs across policies for mix/cores/seed {key}: {hs}")
    print(f"PASS  pairing: {sum(len(h) == 1 for h in hashes.values())}/{len(hashes)} (mix, cores, seed) groups share one schedule")

    failed = batch / "failed_runs.csv"
    if failed.exists():
        lines = failed.read_text().strip().splitlines()[1:]
        report("WARN", f"failed_runs.csv lists {len(lines)} failure(s), and the paper must report them: " + "; ".join(lines[:5]))
    if cfg.get("noise_floor"):
        nf = load(batch / "noise_floor" / "meta.json")
        if not nf or nf.get("status") != "ok":
            report("FAIL", "noise_floor run missing or failed")
        else:
            print("PASS  noise_floor present (summarize with: python3 analysis/validate.py --noise <batch>)")

    print(f"\n{batch.name}: {counts['FAIL']} FAIL, {counts['WARN']} WARN")
    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
