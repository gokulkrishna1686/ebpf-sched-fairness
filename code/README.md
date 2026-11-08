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

## Reproducing the paper

The full instructions are in `Code_and_Reproducibility_Guide.pdf` (in `docs/` next to this folder). In short:

```bash
# 1. Regenerate every number, table and figure from the included raw data (no root, ~5 min)
make reproduce

# 2. Set up a machine for new experiments (Ubuntu 26.04 under WSL2, or a VM)
sudo ./setup/provision.sh              # add --vm in a disposable VM
make                                   # build/wlgen, build/schedmon
make selftest                          # loads the BPF programs, prints PASS + schedstat comparison
make test                              # ~5 min: all automatic tests with a PASS/FAIL summary

# 3. Re-run the whole study (about 8 hours; keep the machine awake and plugged in)
make golden                            # tuning → freeze θ → all batches → audits → figures → tables
make golden TS=<timestamp>             # resume an interrupted run
```

`harness/configs/params_frozen.json` holds the values used for the paper. `make golden` tunes on the new
machine and overwrites it; a copy of the paper's values is in `results/summary/params_frozen_20261106-174432.json`.

`make golden` writes figures to `../report/figures/` and tables to `../report/tables/`.

## Testing without root

`make fake` (or `python analysis/fake_data.py`) writes **synthetic** runs in the real formats under
`results/fake/`, so the analysis and plotting can be exercised anywhere. Those numbers are invented.
Never put them in the report.

## Safety

- Run experiments only on a disposable VM, WSL2 instance or lab machine, never on a production host.
- schedmon restores every nice value, slice and `cpu.weight` it changed on exit or signal. The harness verifies this after every run (`restore_check` in `meta.json`) and kills a run that overruns.
- The starvation guard bounds how long any traced thread can wait.
