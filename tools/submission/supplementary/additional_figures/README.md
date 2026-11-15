# Additional figures

Every figure produced by `code/analysis/plots.py` for the golden run, as PNG previews. The report uses five of them (as PDF): `validation` (Fig. 2), `timeline_h1` (Fig. 3), `cdf_M1` (Fig. 4), `waits_M1_x4` (Fig. 5) and `theta_sweep` (Fig. 6).

| Figure | Shows |
|---|---|
| `cdf_M1`, `cdf_M2`, `cdf_M3` | Upper half of the response-time distribution per mix, at 1, 2 and 4 batch threads per core |
| `pareto_main`, `pareto_x4` | Interactive p99 against batch throughput for every policy, relative to B0 (main grid; the second is the 4-threads-per-core row only) |
| `pareto_heldout` | The same for the held-out mix M4 |
| `waits_M1_x4`, `waits_M2_x4`, `waits_M3_x4` | Interactive run-queue wait p99 and the longest batch wait per policy |
| `timeline_h1` | Worst response per 100 ms window in one default-scheduler run, with batch progress |
| `validation` | Per-thread eBPF wait against the kernel's `run_delay` |
| `ablation` | WaitBoost without aging, without wait feedback, and with per-class input |
| `theta_sweep` | WaitBoost-slice with thresholds from 0.25 to 4 ms |
| `overhead` | Batch throughput and interactive p99 with and without the collector |
| `scaling` | Policies on 1, 2 and 3 experiment cores |

`make reproduce` in `code/` regenerates all of them (PDF and PNG) in `code/results/reproduced/figures/`.
