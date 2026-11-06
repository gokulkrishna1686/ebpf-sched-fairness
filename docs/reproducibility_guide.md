# Reproducibility Guide: eBPF-Based Detection and Mitigation of CPU Scheduling Unfairness

*Companion to the paper. Covers the code, its software dependencies, the configuration, the execution steps and how to reproduce every reported result.*

> **DRAFT STATUS.** Every `⟨fill⟩` marker is filled from the golden run: package versions come from `/var/log/fairexp-packages.txt`, and runtimes and batch ids from `results/raw/*/`. Export to PDF once the markers are filled.

---

## 1. What the code does

The study asks whether short, latency-sensitive (interactive) tasks are disadvantaged by CPU-bound batch tasks under Linux's default EEVDF scheduler. It also asks whether a lightweight policy driven by eBPF telemetry can reduce interactive tail latency without unacceptable batch throughput loss or starvation.

| Component | Language | Role |
|---|---|---|
| `code/workload/wlgen.c` | C | Generates the workload. **Interactive** threads serve open-loop request arrivals (Poisson, on/off or periodic) generated from a seed. **Batch** threads run fixed CPU work units. Records the response time of every request and batch progress per 100 ms. |
| `code/bpf/schedmon.bpf.c` | eBPF (C, CO-RE) | Hooks `sched_wakeup`, `sched_wakeup_new`, `sched_switch` and `sched_migrate_task`, and measures each traced thread's run-queue wait, run time, context switches and migrations. |
| `code/bpf/schedmon.c` | C (libbpf) | Loads and attaches the programs, streams wait events, and writes per-thread statistics. It also runs the **WaitBoost** policy (optional). |
| `code/harness/` | Python 3 | Creates the cgroups and cpuset, applies the baselines, launches runs in a randomized order paired by seed, and records `meta.json`. |
| `code/analysis/` | Python 3 | Computes the metrics, bootstrap confidence intervals and paired tests, and produces the figures and LaTeX tables. |

The file formats and command lines are specified in `code/INTERFACES.md`.

### 1.1 Policies compared

| Name | Description | How it is applied |
|---|---|---|
| B0 `default` | Stock EEVDF, nice 0, equal cgroup weights | — |
| B1 `nice` | Batch threads at nice 10 | `wlgen batch --nice 10` |
| B2 `cgweight` | Interactive cgroup weight 400, batch cgroup weight 100 | `cpu.weight` |
| B3 `slice` | Interactive threads request a 0.3 ms EEVDF slice | `sched_setattr(sched_runtime)` |
| `waitboost-nice` | WaitBoost; a level lowers nice by 3 | schedmon `--actuator nice` |
| `waitboost-slice` | WaitBoost; a level halves the slice, but never below 1.25 × the task's measured CPU burst (a pilot showed that slices shorter than the request's work cause mid-request preemption) | schedmon `--actuator slice` |
| `waitboost-cg` | Per-cgroup WaitBoost; a level doubles the interactive `cpu.weight` | schedmon `--actuator cgweight` |

**WaitBoost rules.** Every 10 ms the policy runs these checks in order:
1. **Guard:** a thread that has been runnable but not running for ≥ 100 ms goes to the maximum level.
2. **Revoke:** a boosted thread whose CPU use exceeds the CPU cap loses its boost.
3. **Boost:** a latency-like thread (sleep fraction ≥ 0.5, CPU use ≤ 0.5) whose EWMA wait exceeds θ gains one level.
4. **Decay:** otherwise, the boost drops one level every 50 ms.

The parameters are frozen in `code/harness/configs/params_frozen.json`.

## 2. Platform

| Item | Used for the paper |
|---|---|
| Host | Windows 11, Intel Core Ultra 5 125H, 16 GB RAM |
| Hypervisor | Oracle VirtualBox ⟨fill version⟩, KVM paravirtualization, Hyper-V present (WHP backend) |
| Guest VM | Ubuntu 26.04 LTS Server, kernel ⟨fill `uname -r`⟩, 4 vCPU, 6 GB RAM, 30 GB disk |
| Experiment CPUs | vCPU 1..k through the cgroup cpuset (k = 2 by default); vCPU 0 is reserved for housekeeping |

**Minimum requirements for reproduction:**
- Linux ≥ 6.12, needed for EEVDF custom slices.
- `CONFIG_BPF_SYSCALL`, `CONFIG_DEBUG_INFO_BTF` and `CONFIG_SCHEDSTATS`.
- cgroup v2 with the `cpu` and `cpuset` controllers.
- Root access in a **disposable** VM or lab machine. Never run on a production host.

## 3. Software dependencies

`code/setup/provision.sh` installs everything from the Ubuntu archive and records the exact versions in `/var/log/fairexp-packages.txt`.

| Package | Version | Purpose |
|---|---|---|
| clang | ⟨fill⟩ | Compiles the eBPF program (`-target bpf`) |
| libbpf-dev | ⟨fill⟩ | eBPF loader library (CO-RE, skeleton, ring buffer) |
| bpftool | ⟨fill⟩ | Generates `vmlinux.h` from BTF and the libbpf skeleton |
| gcc / make | ⟨fill⟩ | Builds wlgen and schedmon |
| trace-cmd | ⟨fill⟩ | Raw ftrace capture for validation |
| bpftrace, stress-ng | ⟨fill⟩ | Sanity checks only |
| python3, numpy, pandas, scipy, matplotlib | ⟨fill⟩ | Harness and analysis |
| zstd | ⟨fill⟩ | Packing raw results |

## 4. Setup

1. **Create the VM.** Follow `code/setup/vm/README.md`, which gives the VBoxManage commands and settings. Install Ubuntu Server with OpenSSH.
2. **Get the code** and provision the VM:
   ```bash
   git clone <repo> ~/fairexp && cd ~/fairexp
   sudo ./code/setup/provision.sh --vm
   ```
   The script prints `ok`/`FAIL` for every kernel feature the study relies on.
3. **Build and self-test:**
   ```bash
   cd code && make && make selftest
   ```
   The self-test must print `PASS`, and its eBPF and schedstat totals for the sleeper thread should agree within a few percent.
4. **Snapshot** the VM (`clean-provisioned`). Restoring this snapshot is the rollback path.

## 5. Configuration

The experiment configs live in `code/harness/configs/*.json`. Their keys are documented in `configs/README.md`.

| Config | Factors | Seeds | Runs | Runtime |
|---|---|---|---|---|
| `tuning.json` | M1–M2, contention x2, θ ∈ {0.25, 0.5, 1, 2} ms × {nice, slice} | 1–3 | 54 | ~0.4 h |
| `main.json` | M1–M3 × {x1, x2, x4} × {B0, B1, B2, B3, WB-nice, WB-slice} + x0 reference | 101–105 | 285 | ~3.1 h |
| `heldout.json` | M4 (unseen) × {x1, x2, x4} × the same policies | 101–105 | 95 | ~1.0 h |
| `ablation.json` | x2 × no-aging / no-feedback / θ sweep / per-cgroup | 101–105 | 180 | ~2.0 h |
| `scaling.json` | 1, 2, 3 experiment cores | 101–105 | 75 | ~0.8 h |
| `overhead.json` | No collector / maps only / maps + events | 101–105 | 45 | ~0.5 h |

**Separating tuning from evaluation.** Parameters are chosen only from `tuning.json`, which uses different seeds and only mixes M1–M2. They are then written to `params_frozen.json` and committed under git tag `params-frozen`. All evaluation configs ran from commit ⟨fill commit⟩. The tag is recorded in every `meta.json`.

**Pairing.** For a given seed the arrival schedule is identical across policies. Each `interactive_summary.json` carries a `schedule_hash` that confirms this.

## 6. Reproducing the results

Run all commands from `code/`.

```bash
make run CONFIG=main        # likewise heldout, ablation, scaling, overhead
make validate-trace
```

Each batch lands in `results/raw/<config>_<timestamp>/` and is packed to `.tar.zst`. Analysis runs on any OS with Python ≥ 3.10:

```bash
python analysis/metrics.py results/raw/<main> results/raw/<heldout> results/raw/<ablation> results/raw/<scaling> results/raw/<overhead>
python analysis/stats.py results/summary/*_runs.csv
python analysis/plots.py --main <main> --heldout <heldout> --ablation <ablation> --overhead <overhead> --scaling <scaling>
python analysis/make_tables.py --main <main> --heldout <heldout> --overhead <overhead> --ablation <ablation>
python analysis/validate.py --trace results/raw/<validation> --threads results/summary/<main>_threads.csv
```

### 6.1 Where each paper artifact comes from

| Paper artifact | File | Produced by |
|---|---|---|
| Response-time CDFs | `report/figures/cdf_M{1,2,3}.pdf` | `plots.py --main` |
| Pareto fronts (p99 vs throughput) | `pareto_main.pdf`, `pareto_heldout.pdf` | `plots.py --main / --heldout` |
| Unfairness episodes (H1) | `timeline_h1.pdf` | `plots.py --main` |
| Run-queue waits by class | `waits_M*_x4.pdf` | `plots.py --main` |
| Ablations, θ sweep | `ablation.pdf`, `theta_sweep.pdf` | `plots.py --ablation` |
| Overhead | `overhead.pdf`, `tab_overhead.tex` | `plots.py --overhead`, `make_tables.py --overhead` |
| Core scaling | `scaling.pdf` | `plots.py --scaling` |
| Validation | `validation.pdf`, `validation_hand.md` | `plots.py --main`, `validate.py --trace` |
| Main and held-out tables | `tab_main.tex`, `tab_heldout.tex` | `make_tables.py` |
| Platform table | `tab_repro.tex` | `make_tables.py --main` (from `sysinfo.json`) |
| Numbers in the text | `report/tables/numbers.tex` | `make_tables.py` |

### 6.2 Batches used in the paper

| Config | Batch id | Commit | Failed runs |
|---|---|---|---|
| main | ⟨fill⟩ | ⟨fill⟩ | ⟨fill, from failed_runs.csv⟩ |
| heldout | ⟨fill⟩ | | |
| ablation | ⟨fill⟩ | | |
| scaling | ⟨fill⟩ | | |
| overhead | ⟨fill⟩ | | |

## 7. Checking a reproduction

- **Collector correctness:**
  - `validate.py --trace` matches the eBPF waits against raw ftrace waits, with a reported median and maximum difference.
  - `validate.py --threads` compares every thread's total eBPF wait with the kernel's own schedstat `run_delay`, giving the aggregate error per class.
- **Event loss:** `events_dropped` in `schedmon_stats.json`, reported per batch (`drop_rate` in `*_runs.csv`).
- **Safety:** `meta.json → restore_check.ok` must be true for every run. `schedmon_stats.json → restore_errors` must be 0.
- **Expected variation:** a re-run on similar hardware should put the medians inside the reported 95% CIs. Absolute latencies depend on the host and hypervisor; the comparisons against B0 are the claims.

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `failed to load BPF programs` / `EPERM` | Not running as root. Use `sudo` |
| `bpftool btf dump` fails | Kernel lacks BTF (`/sys/kernel/btf/vmlinux`). Use the stock Ubuntu kernel |
| `wlgen: cannot join cgroup` | Not root, or the cpuset/cpu controllers are not enabled. Rerun `provision.sh` and check `cgroup.controllers` |
| `slice_readback_ns` is not the requested value | Kernel older than 6.12. Custom slices are unsupported |
| `/usr/bin/env: 'bash\r'` | Windows line endings. The repo's `.gitattributes` forces LF: re-clone, or run `git config core.autocrlf input` and check out again |
| Very noisy latencies | Host load or the WHP backend. Close host apps, use AC power with the Best performance power mode, and keep `isolate_housekeeping` on. See `setup/vm/README.md` §7 |

## 9. Known limitations

- The measurements come from one VM on one host with a hybrid (P/E-core) CPU and one kernel version.
- The VirtualBox WHP backend adds timing noise, which the noise-floor run quantifies.
- The workloads are synthetic. The WaitBoost parameters are tuned on M1–M2 and generalization is checked only on M4.

## 10. Third-party software

The study uses Linux (GPL-2.0), libbpf (LGPL-2.1/BSD-2), bpftool, clang/LLVM (Apache-2.0 with LLVM exception), trace-cmd (GPL-2.0), numpy, pandas, scipy and matplotlib (BSD/PSF-style licenses). The eBPF program is GPL-2.0, as the kernel's helper licensing requires. The rest of the code is MIT.
