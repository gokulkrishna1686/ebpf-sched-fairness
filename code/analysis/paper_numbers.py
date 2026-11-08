#!/usr/bin/env python3
"""Print every number the paper's text quotes that is not already in a table or figure.

  python3 analysis/paper_numbers.py [--summary results/summary] [--ts 20261106-174432]

Reads only the summary CSVs. Used by tests/reproduce_paper.sh.
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

pd.set_option("display.width", 200)
KEY = ["mix", "contention", "seed"]


def pooled(runs, label):
    """Each policy against B0 over all (mix, contention, seed) pairs: median ratio and Wilcoxon test."""
    print(f"\n=== {label}: pooled seed pairs vs B0 (lower p99 is better)")
    base = runs[runs.policy == "default"].set_index(KEY)
    for pol in sorted(p for p in runs.policy.unique() if p != "default"):
        g = runs[runs.policy == pol].set_index(KEY)
        k = g.index.intersection(base.index)
        for metric in ("rt_p99_us", "batch_tput"):
            lr = np.log(g.loc[k, metric].to_numpy() / base.loc[k, metric].to_numpy())
            p = sps.wilcoxon(lr).pvalue if np.any(lr != 0) else 1.0
            print(f"  {pol:28s} {metric:10s} n={len(k):2d}  median ratio {np.exp(np.median(lr)):.3f}  "
                  f"fraction better {np.mean(lr < 0):.2f}  Wilcoxon p={p:.2g}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    code = Path(__file__).resolve().parents[1]
    ap.add_argument("--summary", default=str(code / "results" / "summary"))
    ap.add_argument("--ts", default="20261106-174432")
    args = ap.parse_args()
    S, ts = Path(args.summary), args.ts

    def csv(batch, kind):
        return pd.read_csv(S / f"{batch}_{ts}_{kind}.csv")

    cells, runs = csv("main", "cells"), csv("main", "runs")
    held_cells, held_runs = csv("heldout", "cells"), csv("heldout", "runs")
    loaded, held_loaded = runs[runs.contention != "x0"], held_runs[held_runs.contention != "x0"]

    print("=== H1: default EEVDF per cell (x0 = no batch work)")
    cols = ["rt_p99_us", "rt_p99_slowdown", "slo_viol", "bad_window_frac", "episode_longest_ms",
            "wait_inter_p99_us", "wait_batch_max_us", "starved_win_frac"]
    print(cells[cells.policy == "default"].set_index(["mix", "contention"])[cols].round(3).to_string())

    all_cells = pd.concat([cells[cells.contention != "x0"], held_cells[held_cells.contention != "x0"]])
    print("\n=== fairness over all loaded cells and policies")
    print("  lowest Jain index of normalized shares:", round(cells[cells.contention != "x0"].jain_norm_share.min(), 3))
    print("  largest starved-window fraction:", all_cells.starved_win_frac.max())

    print("\n=== interactive CPU share (fraction of the experiment CPUs, median)")
    print(" ", pd.concat([loaded, held_loaded]).groupby("mix").inter_cpu_frac.median().round(3).to_dict())

    print("\n=== fraction of interactive switches that are involuntary, by contention and policy")
    print(loaded.pivot_table(index="policy", columns="contention", values="invol_frac_inter", aggfunc="median").round(4).to_string())

    print("\n=== WaitBoost actions (median per run) and error totals")
    wb = loaded[loaded.policy.str.startswith("waitboost")]
    print(wb.groupby(["policy", "contention"])[["act_boost", "act_decay", "act_guard", "act_revoke"]].median().to_string())
    print("  guard activations:", int(wb.act_guard.sum()), " actuator errors:", int(loaded.actuator_errors.sum()),
          " restore errors:", int(loaded.restore_errors.sum()),
          f" events dropped: {int(loaded.events_dropped.sum())} of {int(loaded.events_emitted.sum())}")

    pooled(loaded, "main grid")
    pooled(held_loaded, "held-out mix")

    for batch in ("main", "heldout"):
        p = csv(batch, "paired")
        print(f"\n=== {batch}: B2 (class weight) p99 / B0 [95% CI], throughput / B0, SLO misses / B0")
        c = ["mix", "contention", "rt_p99_us_ratio", "rt_p99_us_ratio_lo", "rt_p99_us_ratio_hi", "batch_tput_ratio", "slo_viol_ratio"]
        print(p[p.policy == "cgweight"][c].round(3).to_string(index=False))

    print("\n=== longest batch wait under B2 relative to B0, and its largest value (ms)")
    piv = all_cells.pivot_table(index=["mix", "contention"], columns="policy", values="wait_batch_max_us")
    print((piv["cgweight"] / piv["default"]).round(2).to_string())
    print("  largest under B2:", round(piv["cgweight"].max() / 1e3, 1))

    print("\n=== M1 at x4: run-queue waits (ms)")
    m = cells[(cells.mix == "M1") & (cells.contention == "x4")].set_index("policy")
    print((m[["wait_inter_p99_us", "wait_batch_max_us"]] / 1e3).round(2).to_string())

    abl = csv("ablation", "paired")
    print("\n=== ablations: p99 / B0 per mix (x2)")
    print(abl.pivot_table(index="policy", columns="mix", values="rt_p99_us_ratio").round(2).to_string())
    ablr = csv("ablation", "runs")
    pooled(ablr[ablr.policy.isin(["default", "waitboost-cg"])], "ablation, WB-cg")

    print("\n=== core scaling: p99 / B0, and B0's own p99 (ms)")
    sc = csv("scaling", "paired")
    print(sc.pivot_table(index="policy", columns="cores", values="rt_p99_us_ratio").round(2).to_string())
    sb = csv("scaling", "cells")
    print((sb[sb.policy == "default"].set_index("cores").rt_p99_us / 1e3).round(1).to_dict())

    print("\n=== eBPF vs kernel run_delay, loaded runs (absolute error per thread)")
    t = csv("main", "threads")
    t = t[~t.run_id.str.contains("_x0_")]
    for role, g in t.groupby("role"):
        rel = (g.ebpf_wait_ns - g.schedstat_delay_ns).abs() / g.schedstat_delay_ns
        agg = (g.ebpf_wait_ns.sum() - g.schedstat_delay_ns.sum()) / g.schedstat_delay_ns.sum()
        print(f"  {role}: n={len(g)}  median {rel.median() * 100:.3f}%  aggregate {agg * 100:+.3f}%")
    t0 = csv("main", "threads")
    t0 = t0[t0.run_id.str.contains("_x0_")]
    d = (t0.ebpf_wait_ns - t0.schedstat_delay_ns) / 1e6
    print("  idle reference threads: eBPF minus kernel, per-mix median (ms):",
          d.groupby(t0.run_id.str[:2]).median().round(1).to_dict())


if __name__ == "__main__":
    main()
