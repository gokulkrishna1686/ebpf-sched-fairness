# Interfaces (frozen contract between components)

Every component reads and writes only the files described here. Change this file first, then update the
code, so that the harness and analysis never fall out of sync.

## Clocks and units

- Every timestamp is **CLOCK_MONOTONIC in nanoseconds**. `bpf_ktime_get_ns()` in the eBPF programs, `clock_gettime(CLOCK_MONOTONIC)` in C and `time.monotonic_ns()` in Python all read this same clock.
- Durations are in nanoseconds unless the column name says otherwise (`_us`, `_ms`, `_s`).
- Task classes: `0 = other`, `1 = interactive`, `2 = batch`. The class is decided by cgroup membership.

## Run directory layout

```
results/raw/<batch_id>/<run_id>/
  meta.json               harness   – everything needed to reproduce this run
  requests.csv            wlgen     – one row per interactive request
  interactive_summary.json wlgen
  batch.csv               wlgen     – one row per batch thread (measured window only)
  batch_ts.csv            wlgen     – batch units per thread per 100 ms window (measured window only)
  batch_summary.json      wlgen
  tasks.csv               schedmon  – one row per traced thread
  events.csv              schedmon  – one row per run-queue wait (optional, --events)
  policy_log.csv          schedmon  – one row per boost-level change (policy runs only)
  schedmon_stats.json     schedmon
  *.stdout / *.stderr     harness   – captured process output
```

`run_id` = `<mix>_<contention>_<policy>_c<cores>_s<seed>`, e.g. `M1_x2_waitboost-slice_c2_s101`.
`batch_id` = `<experiment>_<YYYYmmdd-HHMMSS>`, e.g. `main_20261105-220000`.

### requests.csv (wlgen interactive)

| column | meaning |
|---|---|
| task | interactive worker index (0..N-1) |
| tid | Linux thread id of that worker |
| req | request index within the task |
| t_arrival_ns | *intended* arrival time, taken from the seeded schedule |
| t_start_ns | time the worker started serving the request |
| t_end_ns | completion time |
| work_iters | CPU work, in calibrated spin iterations |
| measured | 1 if the arrival falls inside the measured window, 0 during warm-up |

Response time = `t_end_ns - t_arrival_ns`. The generator is open-loop, so this includes queueing behind
earlier requests and avoids coordinated omission. Wake-up lag = `t_start_ns - t_arrival_ns` when the worker was idle.

### batch.csv (wlgen batch)

| column | meaning |
|---|---|
| thread | batch worker index |
| tid | Linux thread id |
| units | work units completed inside the measured window |
| cpu_ns | thread CPU time consumed inside the measured window |
| wall_ns | length of the window as observed by this thread |

### batch_ts.csv (wlgen batch)

The columns are `thread,win,t_win_start_ns,units`. Each row is the number of work units a batch thread completed in one 100 ms window. Only windows that lie fully inside the measured interval are written. These rows drive temporal fairness: the per-window Jain index across batch threads, and starvation windows (a window in which a thread completed zero units).

### Thread entries in *_summary.json (wlgen)

Each thread entry has `idx`, `tid`, `nice_readback`, `slice_readback_ns` and `setup_errno`. It also records the kernel's own accounting, read from `/proc/thread-self/schedstat` just before the thread exits: `schedstat_run_ns`, `schedstat_delay_ns` (time spent waiting on a run queue) and `schedstat_slices`. These give an independent cross-check of `tasks.csv` in every run.

### tasks.csv (schedmon)

`tid,tgid,comm,class,n_wakeups,n_wait,wait_sum_ns,wait_max_ns,run_ns,nvcsw,nivcsw,migrations,h00..h26`

- `n_wait` / `wait_*`: run-queue waits (wake-up → run, or preemption → run again).
- `run_ns`: on-CPU time seen by the tracer.
- `nvcsw` / `nivcsw`: voluntary and involuntary switches out.
- `hNN`: log2 histogram of waits in µs. Bucket 0 is < 1 µs. Bucket k (k ≥ 1) is [2^(k-1), 2^k) µs. The last bucket also takes everything larger.

Counters cover the **whole run, warm-up included**. Use `events.csv` for windowed analysis.

### events.csv (schedmon)

`ts_ns,tid,class,cpu,wait_ns,kind`

- `ts_ns`: when the task got the CPU.
- `kind`: `0` = after a wake-up, `1` = after a preemption (the task was still runnable), `2` = a newly forked task.

### policy_log.csv (schedmon --policy waitboost)

`t_ns,tid,class,action,level_from,level_to,wait_ewma_ns,sleep_frac,cpu_frac`

`action` ∈ `boost, decay, guard, revoke, restore`.

### schedmon_stats.json

Contains `events_emitted`, `events_dropped`, `tracked_tasks`, `duration_ns`, `ticks`, `policy` (the effective parameters),
`actuator_errors`, `base_slice_ns` and `bpf_progs` (per-program `run_cnt` and `run_time_ns` when `kernel.bpf_stats_enabled=1`).

### meta.json (harness)

Contains `run_id`, `batch_id`, `created`, `git` (`commit`, `dirty`), `sysinfo` (the output of `setup/sysinfo.py`), and `config`
(`mix`, `contention`, `cores`, `policy`, `actuator`, `params`, `seed`, `warmup_s`, `duration_s`, `iters_per_us`).
It also contains `cpuset`, `cgroups`, `commands` (the exact argv of every process), `t0_ns`, `t_meas_start_ns`, `t_meas_end_ns`, `status` (`ok` or `failed:<reason>`) and `restore_check`.

## Command lines

### wlgen

```
wlgen calibrate [--trials N]                              → JSON on stdout: {"iters_per_us": ...}
wlgen interactive --tasks N --arrival poisson|onoff|periodic
                  [--rate R] [--on-ms X --off-ms Y] [--period-us P --jitter-us J]
                  --work-us W [--work-dist fixed|lognormal --work-sigma S]
                  COMMON
wlgen batch       --threads N --unit-us U COMMON
COMMON = --seed S --start-at T0_NS --warmup-s W --duration-s D --out DIR
         [--cgroup PATH] [--nice N] [--slice-us U] [--cpus LIST] [--iters-per-us F]
```

### schedmon

```
schedmon --cg-interactive PATH --cg-batch PATH --out DIR
         [--duration-s D] [--events] [--policy none|waitboost]
         [--actuator nice|slice|cgweight] [--interval-ms 10] [--theta-us 500]
         [--alpha 0.3] [--min-sleep 0.5] [--cpu-cap 0.5] [--max-level 3]
         [--decay-ms 50] [--guard-ms 100] [--nice-step 3] [--no-aging] [--no-feedback]
schedmon --selftest            (load, attach, count events for 1 s, print, exit)
```

schedmon exits cleanly on SIGINT or SIGTERM, or after `--duration-s`. On exit it restores every nice value, slice and
`cpu.weight` it changed.

## Configuration files (harness/configs/*.json)

See `harness/configs/README.md`. A config names its mixes, contention levels, policies, seeds, core counts and timing.
`run_matrix.py` expands a config into a run list and executes it in randomized block order.
