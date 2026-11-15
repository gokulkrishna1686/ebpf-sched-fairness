# Experimental results

All results come from one golden run, timestamp `20261106-174432` (734 runs, 7 h 50 min, every stage passed, no failed or interrupted runs).

| Path | Content |
|---|---|
| `summary/<batch>_20261106-174432_runs.csv` | One row of metrics per run (batches: tuning, main, heldout, ablation, scaling, overhead) |
| `summary/<batch>_20261106-174432_cells.csv` | Median and 95% bootstrap CI per mix, contention level and policy |
| `summary/<batch>_20261106-174432_paired.csv` | Each policy's ratio to the default scheduler (B0) over seed pairs, with CI and Wilcoxon p |
| `summary/<batch>_20261106-174432_threads.csv` | Per-thread eBPF wait against the kernel's own `run_delay` (validation) |
| `summary/validation_20261106-174432.txt` | Trace validation, per-thread cross-check and noise-floor results |
| `summary/golden_20261106-174432.log` | Full log of the golden run, with the PASS/FAIL summary at the end |
| `summary/tuning_20261106-174432_choice.txt`, `summary/params_frozen_20261106-174432.json` | How the WaitBoost threshold was chosen, and the frozen parameters |
| `tables/` | Tables I–IV of the report (`tab_repro`, `tab_overhead`, `tab_matrix`, `tab_ablation`) and the full per-cell tables for the main and held-out grids (`tab_main`, `tab_heldout`) |
| `paper_numbers.txt` | Every number quoted in the report's text that is not in a table or figure, computed from the summaries |

These files are copies. The originals live in `code/results/summary/`, and `make reproduce` in `code/` regenerates all of them from the raw data and checks that they match. Column definitions are in `code/INTERFACES.md`; the metric definitions are in the reproducibility guide (§6).
