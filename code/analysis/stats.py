"""Aggregation across repeated runs: medians with 95% bootstrap CIs, and paired comparisons.

Paired comparisons use runs with the same (mix, contention, cores, seed). Same seed means
the same arrival schedule (see the schedule hash in interactive_summary.json), so each
policy is compared against the baseline on identical input. For every pair we take the
ratio policy/baseline, then report the median ratio, its bootstrap CI and an exact
Wilcoxon signed-rank test on the log ratios. With n = 5 pairs the smallest attainable
two-sided p is 0.0625, so the CIs carry the weight of the argument.

  python analysis/stats.py results/summary/main_..._runs.csv
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

CELL = ["batch_id", "mix", "contention", "cores", "policy"]
PAIR = ["batch_id", "mix", "contention", "cores", "seed"]

METRICS = [
    "rt_p50_us", "rt_p95_us", "rt_p99_us", "rt_p999_us", "rt_max_us", "slo_viol", "lag_p99_us",
    "episodes", "bad_window_frac", "episode_longest_ms",
    "batch_tput", "jain_batch", "jain_batch_win_mean", "jain_batch_win_p5", "starved_win_frac",
    "cpu_util", "jain_norm_share", "batch_norm_share",
    "wait_inter_p50_us", "wait_inter_p99_us", "wait_inter_max_us", "wait_inter_wakeup_p99_us",
    "wait_batch_p50_us", "wait_batch_p99_us", "wait_batch_max_us", "starve_batch_n",
    "csw_inter_per_s", "csw_batch_per_s", "migr_batch_per_s",
    "drop_rate", "bpf_ns_per_event", "bpf_cpu_frac", "rt_p99_slowdown",
]
RATIO_METRICS = ["rt_p50_us", "rt_p99_us", "rt_p999_us", "slo_viol", "batch_tput", "wait_inter_p99_us",
                 "wait_batch_p99_us", "wait_batch_max_us", "jain_norm_share", "jain_batch_win_mean"]


def bootstrap_ci(x, stat=np.median, n_boot=10000, alpha=0.05, seed=0):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan, np.nan, np.nan
    if len(x) == 1:
        return float(x[0]), float(x[0]), float(x[0])
    rng = np.random.default_rng(seed)
    boots = stat(rng.choice(x, size=(n_boot, len(x)), replace=True), axis=1)
    return float(stat(x)), float(np.quantile(boots, alpha / 2)), float(np.quantile(boots, 1 - alpha / 2))


def add_slowdown(runs):
    """p99 response time relative to the no-batch (x0) reference run of the same mix, cores, seed."""
    ref = runs[runs.contention == "x0"].set_index(["batch_id", "mix", "cores", "seed"])["rt_p99_us"]
    if ref.empty or "rt_p99_us" not in runs:
        return runs
    key = pd.MultiIndex.from_frame(runs[["batch_id", "mix", "cores", "seed"]])
    runs = runs.copy()
    runs["rt_p99_slowdown"] = runs["rt_p99_us"].to_numpy() / ref.reindex(key).to_numpy()
    return runs


def aggregate(runs, metrics=METRICS):
    rows = []
    for key, g in runs.groupby(CELL, sort=False):
        row = dict(zip(CELL, key))
        row["n"] = len(g)
        for mtr in metrics:
            if mtr in g:
                med, lo, hi = bootstrap_ci(g[mtr])
                row[mtr], row[mtr + "_lo"], row[mtr + "_hi"] = med, lo, hi
        rows.append(row)
    return pd.DataFrame(rows)


def paired(runs, baseline="default", metrics=RATIO_METRICS):
    base = runs[runs.policy == baseline].set_index(PAIR)
    rows = []
    for (bid, mix, cont, cores, pol), g in runs[runs.policy != baseline].groupby(CELL):
        g = g.set_index(PAIR)
        common = g.index.intersection(base.index)
        if len(common) == 0:
            continue
        row = {"batch_id": bid, "mix": mix, "contention": cont, "cores": cores, "policy": pol,
               "baseline": baseline, "n_pairs": len(common)}
        for mtr in metrics:
            if mtr not in g or mtr not in base:
                continue
            a = g.loc[common, mtr].to_numpy(dtype=float)
            b = base.loc[common, mtr].to_numpy(dtype=float)
            ok = np.isfinite(a) & np.isfinite(b) & (b > 0) & (a > 0)
            ratio = a[ok] / b[ok]
            med, lo, hi = bootstrap_ci(ratio)
            row[mtr + "_ratio"], row[mtr + "_ratio_lo"], row[mtr + "_ratio_hi"] = med, lo, hi
            if ok.sum() >= 2 and np.any(np.log(ratio) != 0):
                row[mtr + "_p"] = float(sps.wilcoxon(np.log(ratio), method="exact").pvalue)
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs_csv", nargs="+")
    ap.add_argument("--baseline", default="default")
    args = ap.parse_args()
    for path in args.runs_csv:
        path = Path(path)
        runs = add_slowdown(pd.read_csv(path))
        stem = path.name.replace("_runs.csv", "")
        runs.to_csv(path, index=False)
        cells = aggregate(runs)
        cells.to_csv(path.with_name(stem + "_cells.csv"), index=False)
        pr = paired(runs, args.baseline)
        pr.to_csv(path.with_name(stem + "_paired.csv"), index=False)
        print(f"{path.name}: {len(cells)} cells, {len(pr)} paired comparisons", file=sys.stderr)


if __name__ == "__main__":
    main()
