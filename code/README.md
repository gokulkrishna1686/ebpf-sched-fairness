# fairexp: eBPF detection and mitigation of CPU scheduling unfairness

This repository is the code for an OS course study. It asks whether short, latency-sensitive tasks are
disadvantaged by CPU-bound batch work on Linux's EEVDF scheduler. It also tests whether a lightweight,
telemetry-driven policy (**WaitBoost**) can cut interactive tail latency without starving the batch work.
Everything runs on an unmodified kernel (≥ 6.12). No sched_ext and no kernel patches are needed.

```
wlgen (C)          seeded open-loop interactive requests + CPU-bound batch units
   │  threads live in /sys/fs/cgroup/fairexp/{interactive,batch}
   ▼
schedmon.bpf.c     tp_btf sched_wakeup / sched_wakeup_new / sched_switch / sched_migrate_task
   │  per-thread wait/run maps + ring buffer of run-queue waits
   ▼
schedmon (C)       dumps tasks.csv / events.csv; optional WaitBoost policy loop (10 ms):
                   EWMA wait + interactivity test → bounded, aged, starvation-guarded boost
                   via nice | EEVDF slice (sched_attr.sched_runtime) | cgroup cpu.weight
   ▼
harness (Python)   cgroups/cpuset, baselines B0–B3, randomized paired runs, meta.json
   ▼
analysis (Python)  metrics → bootstrap CIs + paired tests → figures + LaTeX tables
```

## Layout

| Path | What |
|---|---|
| `INTERFACES.md` | **The contract**: every file format and command line |
| `setup/` | `provision.sh` (packages and checks), `sysinfo.py` (platform record), `vm/README.md` (VirtualBox VM) |
| `workload/wlgen.c` | Workload generator (interactive / batch / calibrate) |
| `bpf/` | `schedmon.bpf.c` (eBPF), `schedmon.c` (loader, dumper, WaitBoost), `schedmon.h` |
| `common/sched_attr.h` | `sched_setattr` helpers (nice + EEVDF custom slice) |
| `harness/` | `run_matrix.py` (entry point), `run_experiment.py`, `cgroups.py`, `baselines.py`, `validate_trace.py`, `configs/` |
| `analysis/` | `metrics.py`, `stats.py`, `plots.py`, `make_tables.py`, `validate.py`, `fake_data.py`, `style.py`, `load.py` |
| `results/raw/` | One directory per batch (packed to `.tar.zst` for git) |
| `results/summary/` | `<batch>_{runs,cells,paired,threads}.csv` |

## Quick start (inside the Ubuntu 26.04 VM or WSL2)

```bash
sudo ./setup/provision.sh --vm         # drop --vm on WSL2
make                                   # build/wlgen, build/schedmon
make selftest                          # loads the BPF programs, prints PASS + schedstat comparison
make quick                             # ~2 min: 8 runs → metrics → figures in results/quick_figures/
```

## Full study (in this order)

```bash
make run CONFIG=tuning                 # seeds 1-3, M1-M2 only
#   pick θ etc. → edit harness/configs/params_frozen.json → git commit → git tag params-frozen
make run CONFIG=main                   # ~3.1 h
make run CONFIG=heldout                # ~1.0 h
make run CONFIG=ablation               # ~2.0 h
make run CONFIG=scaling                # ~0.8 h
make run CONFIG=overhead               # ~0.5 h
make validate-trace                    # hand-checkable trace vs eBPF
```

Then analyze. This runs on Windows or Linux, and needs only numpy, pandas, scipy and matplotlib:

```bash
python analysis/metrics.py results/raw/<main_id> results/raw/<heldout_id> ...
python analysis/stats.py results/summary/<id>_runs.csv ...
python analysis/plots.py --main <main_id> --heldout <id> --ablation <id> --overhead <id> --scaling <id>
python analysis/make_tables.py --main <main_id> --heldout <id> --overhead <id> --ablation <id>
python analysis/validate.py --trace results/raw/validation_<ts> --threads results/summary/<main_id>_threads.csv
```

Figures go to `../report/figures/` and tables to `../report/tables/`. The paper quotes numbers only through
the macros in `tables/numbers.tex`.

## Testing without root

`make fake` (or `python analysis/fake_data.py`) writes **synthetic** runs in the real formats under
`results/fake/`, so the analysis and plotting can be exercised anywhere. Those numbers are invented.
Never put them in the report.

## Safety

- Experiments run only in the disposable VM.
- schedmon restores every nice value, slice and `cpu.weight` it changed on exit or signal. The harness verifies this after every run (`restore_check` in `meta.json`) and kills a run that overruns.
- The starvation guard bounds how long any traced thread can wait.
