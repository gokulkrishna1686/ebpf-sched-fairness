# Reproducibility Guide

**eBPF-Based Detection and Mitigation of CPU Scheduling Unfairness Under Mixed Workloads**
Team 10, Assignment A2. Gokul Krishna Balaji (CS24B2053), Garikina Asish Preetam (CS24I1022), Bhyrapuneni Venkat Manoj (CS24B2052), Karra Sunith Vardhan Singh (CS24B2017). Indian Institute of Information Technology, Design and Manufacturing, Kancheepuram.

This guide is part of `Team_10_Assignment_A2.zip`. It explains how the code is organized, what it needs, how to set it up and run it, what it produces, and how to reproduce every result in the report. The same content is in `documentation/reproducibility_guide.pdf`.

| Goal | What to do | Needs |
|---|---|---|
| Check the reported results | Regenerate every number, table and figure from the included raw data with `make reproduce` (§5.1) | No root, about 2 min |
| Check that the code runs | Build and run the automatic tests (§3, §5.2) | Root, about 5 min |
| Repeat the experiments | Run the whole study again with `make golden` (§5.3) | Root, about 8 h |

## 1. Project overview and organization

The project measures how long each thread waits on the Linux run queue, using eBPF, and uses that measurement to study and reduce CPU scheduling unfairness between short interactive tasks and CPU-bound batch tasks under Linux's EEVDF scheduler. It compares the default scheduler with three static settings and with **WaitBoost**, a feedback policy driven by the measured waits.

### 1.1 Archive layout

```
Team_10_Assignment_A2.zip
  README.md                       overview and quick start
  latex_report/                   report.pdf, main.tex, references.bib, IEEEtran class and style,
                                  figures/, other_latex_files/ (tables used by main.tex)
  code/                           everything needed to build, run and analyse (below)
  documentation/                  reproducibility_guide.pdf and reproducibility_guide.md
  supplementary/
    datasets/                     note: the workloads are generated; there is no external dataset
    experimental_results/         summary CSVs, logs, all tables, numbers quoted in the report
    additional_figures/           all figures, including those not in the report
    other_supporting_materials/   hand-checkable trace validation
```

### 1.2 The code folder

The code keeps the layout it was run with, because the scripts refer to these paths.

| Role | Location in `code/` |
|---|---|
| Source | `workload/wlgen.c`, `bpf/` (eBPF programs and loader), `common/`, `harness/*.py`, `analysis/*.py` |
| Scripts | `Makefile` (all entry points), `tests/` (tests, full study, reproduction), `setup/` (provisioning) |
| Configuration | `harness/configs/` (experiment definitions, workload mixes, frozen parameters) |
| Dependencies | `requirements.txt` (Python), `setup/provision.sh` (system packages) |
| Data and results | `results/raw/` (raw data of the reported run, packed), `results/summary/` (CSV summaries) |
| Interfaces | `INTERFACES.md` (every file format and command-line option), `README.md` |

### 1.3 Components

| Path | Language | Role |
|---|---|---|
| `workload/wlgen.c` | C | Generates the workload. Interactive threads serve open-loop request arrivals (Poisson, on/off or periodic) drawn from a seed. Batch threads run fixed CPU work units. Records every request's response time and batch progress per 100 ms. |
| `bpf/schedmon.bpf.c` | eBPF (CO-RE) | Hooks `sched_wakeup`, `sched_wakeup_new`, `sched_switch` and `sched_migrate_task`. Measures each traced thread's run-queue wait, run time, context switches and migrations. |
| `bpf/schedmon.c` | C (libbpf) | Loads the programs, streams wait events, writes per-thread statistics and optionally runs WaitBoost. |
| `harness/` | Python 3 | Creates the cgroups and cpuset, applies each policy, launches runs paired by seed in randomized order, records `meta.json`. Entry point: `run_matrix.py`. |
| `analysis/` | Python 3 | `metrics.py`, `stats.py` (bootstrap CIs, paired ratios), `plots.py`, `make_tables.py`, `validate.py`, `tune.py` (threshold selection), `paper_numbers.py` (numbers quoted in the text). |
| `tests/` | Bash, Python | `run_all.sh` (automatic tests), `golden.sh` (full study), `check_batch.py` (batch audit), `reproduce_paper.sh` (regeneration from raw data). |

### 1.4 Policies compared

| Name | Description | Mechanism |
|---|---|---|
| B0 `default` | Stock EEVDF, nice 0, equal cgroup weights | none |
| B1 `nice` | Every batch thread at nice 10 | `setpriority` |
| B2 `cgweight` | Interactive cgroup weight 400, batch cgroup weight 100 | `cpu.weight` |
| B3 `slice` | Every interactive thread requests a 0.3 ms EEVDF slice | `sched_setattr` |
| `waitboost-nice` | WaitBoost; each level lowers nice by 3 | `setpriority` |
| `waitboost-slice` | WaitBoost; each level halves the slice, never below 1.25 × the thread's measured CPU burst | `sched_setattr` |
| `waitboost-cg` | Per-class WaitBoost; each level doubles the interactive `cpu.weight` | `cpu.weight` |

WaitBoost applies these rules every 10 ms, in order: (1) **guard**: a thread runnable but not running for at least 100 ms goes to the maximum level (3); (2) **revoke**: a boosted thread whose CPU use exceeds 0.5 loses its boost; (3) **boost**: a thread with sleep fraction ≥ 0.5, CPU use ≤ 0.5 and EWMA wait above θ gains one level; (4) **decay**: otherwise the boost drops one level every 50 ms. On exit every changed knob is restored, and the harness verifies this after each run.

## 2. Hardware and software requirements

**Hardware.** An x86-64 machine with at least 4 logical CPUs (CPU 0 for housekeeping, CPUs 1–3 for experiments), about 6 GB of RAM and 3 GB of free disk.

**Operating system and kernel.** Ubuntu 26.04 (native, in WSL2 or in a VM) on Linux 6.12 or newer (custom EEVDF slices need 6.12). The kernel must have `CONFIG_BPF_SYSCALL`, BTF (`CONFIG_DEBUG_INFO_BTF`, visible as `/sys/kernel/btf/vmlinux`) and cgroup v2 with the `cpu` and `cpuset` controllers, and systemd must be running. No kernel patch and no `sched_ext` are needed. `CONFIG_SCHEDSTATS` is optional: the reported kernel did not have it. Root access is required for new experiments, so use a disposable VM, WSL2 instance or lab machine, never a production host.

**Software dependencies.** `setup/provision.sh` installs everything from the Ubuntu archive; `code/requirements.txt` lists the Python packages for `pip`. Versions used for the reported results:

| Package | Version | Purpose |
|---|---|---|
| clang | 21.1.8 | Compiles the eBPF programs |
| gcc, make | 15.2.0 | Builds `wlgen` and `schedmon` |
| libbpf (`libbpf-dev`) | 1.6.3 | eBPF loader library |
| bpftool | 7.7.0 | Generates `vmlinux.h` and the libbpf skeleton |
| trace-cmd | 3.3.3 | Independent trace for validation |
| zstd | from Ubuntu 26.04 | Packs and unpacks raw results |
| Python | 3.14.4 | Harness and analysis |
| numpy, pandas | 2.3.5, 2.3.3 | Analysis |
| scipy, matplotlib | 1.16.3, 3.10.7 | Statistics, figures |

`provision.sh` also installs bpftrace and stress-ng for manual checks; no reported result uses them. For `make reproduce` alone, Python with the four analysis packages, zstd and tar are enough.

## 3. Installation and environment setup

**1. Linux environment.** On Windows, install WSL2 with an Ubuntu 26.04 distribution (`wsl --list --online` shows the exact name to pass to `wsl --install -d`), and enable systemd (`[boot] systemd=true` in `/etc/wsl.conf`, then `wsl --shutdown`). Native Ubuntu 26.04 or a VM works too; for a VirtualBox VM see `code/setup/vm/README.md`.

**2. Unpack, provision, build.** Unpack the archive inside the Linux file system with `unzip`, not through Windows Explorer or under `/mnt/c`: this keeps the scripts executable and avoids slow disk access.

```bash
sudo apt install -y unzip
unzip Team_10_Assignment_A2.zip -d ~/A2 && cd ~/A2/code
sudo ./setup/provision.sh      # add --vm in a disposable VM
make                           # builds build/wlgen and build/schedmon
make selftest                  # must print PASS
```

`provision.sh` prints `ok` or `FAIL` for every kernel feature the study relies on. With `--vm` it also pins the kernel, enables passwordless sudo and disables noisy system timers, so use that flag only on a disposable VM.

## 4. Configuration parameters and inputs

**Inputs.** The experiments need no input files or external datasets. Each run's input (request arrival times and work amounts) is generated by `wlgen` from the configuration and a seed, so the same seed gives identical input to every policy.

**Experiment definitions** are JSON files in `harness/configs/`; `configs/README.md` documents every key. The most important are `use_mixes`, `contention` (batch threads per core), `policies`, `seeds`, `cores`, `warmup_s`, `duration_s` and `isolate_housekeeping` (confines system services to CPU 0). Experiment CPUs start at CPU 1. Each run has a 5 s warm-up and a 30 s measured window.

| Config | Factors | Seeds | Runs | Time |
|---|---|---|---|---|
| `tuning` | M1–M2, x2; θ ∈ {0.25, 0.5, 1, 2} ms × {nice, slice}, plus B0 | 1–3 | 54 | 24 min |
| `main` | M1–M3 × {x1, x2, x4} × 6 policies, plus x0 reference runs | 101–105 | 285 | 3 h 07 |
| `heldout` | M4 (never tuned on) × {x1, x2, x4} × 6 policies, plus x0 | 101–105 | 95 | 1 h 02 |
| `ablation` | M1–M3, x2: no aging, no feedback, θ sweep, per-class input | 101–105 | 180 | 1 h 59 |
| `scaling` | M1, x2, on 1, 2 and 3 experiment cores | 101–105 | 75 | 49 min |
| `overhead` | M1 × {x1, x2, x4}: no collector, maps only, maps + events | 101–105 | 45 | 29 min |

**Workload mixes** (`configs/mixes.json`). Level x*k* places *k* batch threads on each experiment core; x0 has none.

| Mix | Threads | Arrivals | Work per request | SLO |
|---|---|---|---|---|
| M1 | 2 | Poisson, 100/s each | 1 ms, fixed | 5 ms |
| M2 | 8 | Poisson, 50/s each | 0.5 ms, fixed | 2.5 ms |
| M3 | 4 | On/off: 400/s for 100 ms, then 400 ms idle | Lognormal, median 0.5 ms, σ = 0.5 | 2.5 ms |
| M4 (held out) | 4 | Periodic 60 Hz, ±1 ms jitter | Lognormal, median 2 ms, σ = 0.6 | 16.7 ms |

**Frozen parameters.** `configs/params_frozen.json` holds the WaitBoost parameters: interval 10 ms, EWMA α = 0.3, minimum sleep fraction 0.5, CPU cap 0.5, maximum level 3, decay 50 ms, guard 100 ms, nice step 3, and θ = 2 ms for the nice and slice actuators (0.5 ms for the per-class variant). The archive ships the values used for the reported results. `tune.py` chooses θ using only the tuning seeds, by a rule fixed in advance: the lowest median p99 ratio among values whose median throughput ratio is at least 0.97. `make golden` repeats this tuning on the new machine and overwrites the file before any evaluation run, keeping a copy in `results/summary/params_frozen_<timestamp>.json`.

## 5. Step-by-step execution

### 5.1 Reproducing the reported results from the included raw data (no root)

```bash
cd ~/A2/code
make reproduce
```

This checks the SHA-256 of every archive, unpacks them under `results/raw/`, recomputes all metrics and statistics into `results/reproduced/summary/`, compares them column by column with `results/summary/`, repeats the collector validation, and rebuilds the figures and tables. It must end with `REPRODUCE: PASS` and report that all tables match. With a different NumPy version, the last digits of bootstrap intervals may differ.

### 5.2 Testing the code on your machine

```bash
make test      # about 5 min, needs root
```

Runs the workload generator test, the collector self-test, the policy restore test for each actuator, an end-to-end quick matrix with audit, analysis and figures, and the trace validation, printing PASS or FAIL for each. `make quick` runs only the 2-minute end-to-end matrix; `make pilot` is a 1-hour rehearsal of the whole study with 4 s runs. Pilot and quick figures carry a watermark so they cannot be mistaken for results.

### 5.3 Re-running the whole study

Before starting on a Windows host: plug the laptop in, set the power mode to Best performance, set sleep to Never, pause Windows Update and close other applications. If the host sleeps, WSL or the VM freezes and runs fail.

```bash
tmux new -s golden
make golden                  # about 8 hours
make golden TS=<timestamp>   # resume an interrupted run; finished runs are kept
```

The command tunes, freezes θ, runs all five evaluation batches and the trace validation, audits every batch, analyzes them, and writes figures and tables to `../report/figures` and `../report/tables`. It ends with `GOLDEN RUN: ALL STAGES PASS`; its log is `results/summary/golden_<timestamp>.log`. `tests/check_batch.py` marks as failed any run that took more than three times the typical wall time, since that indicates a host interruption: delete such runs and resume. This decision uses wall time only, never the results. A single batch can be run with `make run CONFIG=main`.

## 6. Output files, results and evaluation metrics

**Per run** (`results/raw/<batch>/<run_id>/`, run ids of the form `<mix>_<contention>_<policy>_c<cores>_s<seed>`): `requests.csv` (arrival, start and end time of every request), `batch.csv` and `batch_ts.csv` (batch work and its progress per 100 ms), `tasks.csv` (per-thread eBPF statistics), `events.csv` (every run-queue wait), `policy_log.csv` (WaitBoost decisions), `schedmon_stats.json` (collector counters, drops, restore result) and `meta.json` (exact commands, configuration, code version, restore check). Each batch also stores `sysinfo.json`, `calibration.json` and `policies.json`. `code/INTERFACES.md` specifies every column.

**Per batch** (`results/summary/`): `<batch>_runs.csv` (one row of metrics per run), `_cells.csv` (median and 95% CI per mix, contention and policy), `_paired.csv` (each policy's ratio to B0 over seed pairs, with CI and Wilcoxon p) and `_threads.csv` (per-thread eBPF wait against the kernel's `run_delay`).

| Metric | Definition |
|---|---|
| Response time | End of a request minus its intended arrival time (open loop, so queueing delays are included) |
| p99, p50 | 99th and 50th percentile of response time over the measured window |
| SLO miss rate | Fraction of requests slower than the mix's SLO |
| Slowdown | p99 with batch work ÷ p99 of the same mix and seed with no batch work (x0) |
| Unfairness episode | Maximal run of consecutive 100 ms windows whose worst response exceeds the SLO |
| Run-queue wait | Time a thread was runnable but not running, measured by eBPF; reported as p99 per class |
| Longest batch wait | Largest single run-queue wait of any batch thread |
| Starved window | A batch thread and 100 ms window with no completed work unit |
| Batch throughput | Completed batch work units per second |
| Normalized share, Jain | CPU time received ÷ max-min fair entitlement; Jain's index (Σnᵢ)² / (N·Σnᵢ²) over all tasks (1 = perfectly fair) |
| Involuntary switch fraction | Share of a thread's switches where it was preempted while still runnable |
| Ratio to B0 | A policy's metric ÷ B0's on the same seed; median over seeds with a 95% bootstrap CI (10,000 resamples) |

### 6.1 Where each result in the report comes from

Paths are relative to `code/results/reproduced/` after `make reproduce`. Copies of the figures, tables and numbers are also in `supplementary/`.

| In the report | Content | File |
|---|---|---|
| Table I | Platform | `tables/tab_repro.tex` |
| Table II | Instrumentation overhead | `tables/tab_overhead.tex` |
| Table III | All policies in all cells | `tables/tab_matrix.tex` |
| Table IV | Ablations | `tables/tab_ablation.tex` |
| Fig. 1 | System diagram (drawn in the LaTeX source) | – |
| Fig. 2 | Per-thread validation | `figures/validation.pdf` |
| Fig. 3 | Unfairness timeline | `figures/timeline_h1.pdf` |
| Fig. 4 | Response-time distribution | `figures/cdf_M1.pdf` |
| Fig. 5 | Run-queue waits | `figures/waits_M1_x4.pdf` |
| Fig. 6 | Threshold sweep | `figures/theta_sweep.pdf` |
| Text, §IV-A | Trace validation and noise floor | `summary/validation_<timestamp>.txt` |
| Text, all of §IV | Pooled statistics and other quoted numbers | `paper_numbers.txt` |

### 6.2 The reported run

All results come from one golden run, timestamp `20261106-174432`, on an Intel Core i5-1155G7 laptop (8 logical CPUs, 7.6 GiB) running Ubuntu 26.04.1 under WSL2, kernel `6.18.54.1-microsoft-standard-WSL2`, `HZ=250`, EEVDF base slice 2.8 ms. It took 7 h 50 min, used code commit `0a95074` (recorded in every `meta.json`) with the frozen parameters above, and finished with every stage passing. No run failed in any batch, and no run was interrupted or re-run. The experiment code in this archive (`bpf/`, `workload/`, `harness/`) is the code of that run.

## 7. Checking a reproduction

- **Collector accuracy.** `validate.py --trace` must print `PASS`: at least 95% of waits matched against the raw trace, median difference at most 5 µs, p99 at most 20 µs. The reported run matched 706 of 706 waits with a median of 0.12 µs. `validate.py --threads` compares each thread's total with the kernel's `run_delay`.
- **Event loss and safety.** `events_dropped` and `restore_errors` must be 0, as they were in the reported run. `check_batch.py` must report 0 FAIL for every batch. It may print WARN when a thread started slightly late; these appear in the reported run too and do not indicate a failure.
- **Platform noise.** `validate.py --noise` measures idle wake-up lag; its verdict should be `OK` (p99 below 1 ms). The reported run had 0.48 ms.
- **Expected variation.** Absolute latencies depend on the machine and hypervisor. The claims are the comparisons against B0. On another machine, B2 should still lower interactive p99 in every cell, and the per-task policies should stay near or above 1. Differences below about 10% are within the noise of five seeds.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Permission denied` on a script | The archive was unpacked by Windows, which drops the executable bits. Unpack with `unzip` inside Linux, or run `chmod +x tests/* setup/*`. |
| `/usr/bin/env: 'bash\r'` | Windows line endings were introduced. Unpack the original archive again inside Linux. |
| Failed to load BPF programs, `EPERM` | Not running as root. The `make` targets call `sudo` where needed. |
| `bpftool btf dump` fails | The kernel has no BTF. Use a stock distribution kernel or the default WSL2 kernel. |
| Cannot join the cgroup | Controllers `cpu` or `cpuset` are missing. Rerun `provision.sh` and check `/sys/fs/cgroup/cgroup.controllers`. |
| `isolate_housekeeping` errors in WSL2 | systemd is off. Set `systemd=true` under `[boot]` in `/etc/wsl.conf`, then run `wsl --shutdown`. |
| Runs fail with `timed out` | The host slept or stalled. Keep it awake, then resume with `make golden TS=<timestamp>`. |
| Noisy latencies, noise verdict not `OK` | Host load. Close other applications, use mains power and Best performance mode, and repeat `make quick`. |

## 9. Known limitations

The measurements come from one laptop under WSL2 and one kernel version. The platform's own wake-up lag (0.48 ms p99) sets a floor under absolute latencies. Five seeds per cell resolve only effects of roughly 10% or more. The workloads are synthetic. Parameters were tuned on M1–M2, and generalization was checked only on M4. The scaling runs did not include B2.

## 10. Third-party software

The project uses Linux (GPL-2.0), libbpf (LGPL-2.1 or BSD-2-Clause), bpftool, clang/LLVM (Apache-2.0 with LLVM exception), trace-cmd (GPL-2.0), and NumPy, pandas, SciPy and Matplotlib (BSD or PSF-style licenses). These are cited in the report. No external dataset is used; all data were generated by this project. The eBPF program is GPL-licensed, as the kernel's helpers require, and the rest of the code is MIT-licensed.
