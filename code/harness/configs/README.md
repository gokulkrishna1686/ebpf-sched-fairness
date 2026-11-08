# Experiment configurations

| File | Purpose | Seeds | Runs (approx.) |
|---|---|---|---|
| `quick.json` | Pipeline smoke test (`make quick`) | 1 | 8 + noise floor |
| `tuning.json` | Threshold tuning, M1–M2 only | 1–3 (tuning) | 54 |
| `main.json` | Main grid: {x1, x2, x4} × {M1, M2, M3} × 6 policies | 101–105 (eval) | 285 + noise floor |
| `heldout.json` | Unseen mix M4 | 101–105 | 95 |
| `ablation.json` | No aging / no feedback / θ sweep / per-cgroup / actuators | 101–105 | 180 |
| `scaling.json` | 1, 2, 3 experiment cores | 101–105 | 75 |
| `overhead.json` | Collector off vs on (with and without events) | 101–105 | 45 |

**Rules.**
- Tune only with `tuning.json`.
- `analysis/tune.py --write` (run automatically by `make golden`) writes the chosen values into `params_frozen.json`. Only then run the evaluation configs. The file in this folder holds the values used for the paper.
- Never select runs after the fact. `failed_runs.csv` lists everything that failed, and the paper reports it.

## Keys

| Key | Meaning |
|---|---|
| `mixes` | `"mixes.json"`, or an inline dict of mix name → `{"interactive": {...}}` (wlgen interactive options) |
| `use_mixes` | Subset of mixes to run |
| `contention` | Name → batch threads per experiment core |
| `policies` | Names from `baselines.POLICIES` or `policies_custom` |
| `policies_custom` | `[{"name", "base", ...overrides}]`; nested `schedmon` options are merged |
| `seeds` | One block per seed. Policies inside a block run in a shuffled order fixed by `order_seed` |
| `cores` | Experiment cores, placed on CPUs `first_cpu` .. `first_cpu + cores - 1` (default first_cpu = 1) |
| `warmup_s`, `duration_s` | Warm-up (discarded) and measured window |
| `reference` | Also run every mix with zero batch threads under `default`, used for slowdown |
| `noise_floor` | `{"mix": "NOISE", "duration_s": 10}`: an idle wake-up latency run at batch start |
| `events` | Stream per-wait events (`events.csv`) |
| `bpf_stats` | Enable `kernel.bpf_stats_enabled` during runs (overhead measurement) |
| `isolate_housekeeping` | Confine system.slice, user.slice and init.scope to the housekeeping CPU (systemd) |
| `iters_per_us` | Skip calibration and use this spin rate |
