# Testing and Results Runbook

How the team tests the code in the VM, then produces the results that go into the paper.

> **New to this?** Start with [**TEST_GUIDE.md**](TEST_GUIDE.md). It's a step-by-step tutorial for every test: what it checks, how it works, how long it takes, example output, and what to do if it fails. This file is the checklist: who does what, the sign-off table and the golden run.
There are three phases:

| Phase | Where | When | Who |
|---|---|---|---|
| 1. Testing (T0–T10) | Each person's own VM | Nov 2–4 | A, B, C, D (assignments below) |
| 2. Golden run | The one designated laptop VM | Nov 5–6 (overnight) | Golden-run owner |
| 3. Analysis and consistency | Windows or VM | Nov 6–8 | C (figures/tables), D (paper) |

**Golden rule:** only Phase 2 numbers go into the paper. Everything in Phase 1 is testing.

**The ladder: short runs first, the long run last.**

| Step | Command | Time | What it proves |
|---|---|---|---|
| 1. Quick smoke | `make quick` | ~3 min | The basics work: BPF loads, the workload runs, the policies apply, figures appear |
| 2. Pilot (dress rehearsal) | `make pilot` | ~1 h | **Every** experiment config, trace validation and the full analysis work end to end (4 s runs, 2 seeds) |
| 3. Golden run | §2 below | ~9 h | The real results (30 s runs, 5 seeds) |

Do not move up a step until the one below passes.

---

## 0. Before you start (everyone)

1. **Build your VM.** Follow [`code/setup/vm/README.md`](../code/setup/vm/README.md) (~45 min the first time).
2. **Get the code and provision the VM** (inside the VM, over SSH):
   ```bash
   git clone git@github.com:<org>/<repo>.git ~/fairexp     # or: cd ~/fairexp && git pull
   cd ~/fairexp
   sudo ./code/setup/provision.sh --vm
   cd code && make
   ```
3. **Use tmux for anything longer than a few minutes.** If your SSH connection drops, the run keeps going:
   ```bash
   tmux new -s fx          # start a session
   # Ctrl-b then d        # detach (the run continues)
   tmux attach -t fx       # come back later
   ```
4. **Run every command below from `~/fairexp/code`** unless stated otherwise.
5. **Record each result** in the sign-off table at the end. For failures, use the bug template in §5.

### Test assignments

| ID | Test | Owner | Root | Time | Needs |
|---|---|---|---|---|---|
| T0 | Environment checks | everyone | yes | 5 min | – |
| T1 | Build | everyone | no | 2 min | T0 |
| T2 | Workload generator smoke test | B | no | 2 min | T1 |
| T3 | Collector selftest (BPF loads, eBPF vs schedstat) | A | yes | 1 min | T1 |
| T4 | Trace validation + **hand check** | A | yes | 30 min | T3 |
| T5 | Policy restore / safety (3 actuators) | B | yes | 5 min | T3 |
| T6 | End-to-end smoke matrix (`make quick`) | C | yes | 5 min | T3 |
| T7 | Failure handling, interrupt and resume | B | yes | 10 min | T6 |
| T8 | Analysis pipeline on real quick data | C | no | 15 min | T6 |
| T9 | Host noise gate (golden laptop only) | golden-run owner | yes | 15 min | T6 |
| T10 | Clean-room rebuild from the docs | D | yes | 1 h | – |
| T11 | **Pilot**: whole study with short runs | C (own VM), then the golden-run owner (golden machine) | yes | ~1 h | T6 |

---

## 1. Phase 1: tests

### T0: Environment checks (everyone)

```bash
sudo ./setup/provision.sh --vm           # skip if you already ran it in §0
make check-kernel
python3 setup/sysinfo.py | head -60
```

**Pass:**
- Every provision check prints `ok`.
- The kernel is ≥ 6.12.
- `BTF: present`.
- The cgroup controllers include `cpu` and `cpuset`.

**Record:** `uname -r`, and the `info sched_ext …` line. It tells us whether the optional sched_ext baseline is possible.

### T1: Build (everyone)

```bash
make clean && make
```

**Pass:** no warnings and no errors, and both `build/wlgen` and `build/schedmon` exist.

### T2: Workload generator smoke test (B)

```bash
./tests/wlgen_smoke.sh
```

**Pass:** the last line is `wlgen_smoke: ALL PASS`. A `WARN` on calibration is allowed. Record the three calibration values.

**What it checks:**
- Same seed → identical arrival schedule.
- The kernel accepts the 100 µs EEVDF slice (needed for WB-slice and B3).
- Batch threads really run at nice 10.
- One CPU is fully used.
- Batch threads progress evenly.

### T3: Collector selftest (A)

```bash
make selftest
```

**Expected output** (the numbers will differ):

```
schedmon selftest: tracking cgroup /sys/fs/cgroup/user.slice/..., pinned to CPU 2
  events emitted=812 dropped=0
  tid 4321    sleeper  wakeups=201 waits=403 mean_wait= 512.3us max_wait=2100.0us ... | schedstat run_delay= 206.4ms ebpf= 206.1ms
  tid 4322    spinner  ...
selftest PASS
```

**Pass:**
- `selftest PASS` with `dropped=0`.
- The sleeper has ≥ 150 waits.
- For the **sleeper** row, the eBPF total is within **5 %** of the schedstat total.

The spinner's schedstat reads 0 because it has already exited. That is expected.

**If the BPF programs fail to load:** run `sudo ./build/schedmon --selftest --verbose 2>&1 | tail -80` and send the output (it contains the verifier log).

### T4: Trace validation and hand check (A)

This is the brief's "validate estimates against a small hand-checked trace".

```bash
make validate-trace
python3 analysis/validate.py --trace results/raw/validation_<timestamp>
```

**Pass (automatic part):**
- ≥ 95 % of trace waits are matched to an eBPF wait.
- The median |difference| is ≤ 5 µs, and the p99 is ≤ 20 µs.

**Hand check (manual part):**
1. Open `results/raw/validation_<timestamp>/validation_hand.md`. It lists 20 waits, each with two line numbers in `trace.txt`.
2. For each row, print both lines and compute the wait yourself:
   ```bash
   sed -n '<runnable line>p;<switch-in line>p' results/raw/validation_<timestamp>/trace.txt
   ```
   Wait = switch-in timestamp − runnable timestamp (in µs).
3. Copy the table to `docs/validation_hand_checked.md` and add a column "by hand (µs)". Your value must equal "trace wait" within ±1 µs and be close to "eBPF wait".
4. Commit `docs/validation_hand_checked.md`. It is evidence for the paper.

### T5: Policy restore / safety (B)

```bash
sudo python3 tests/restore_test.py --actuator nice
sudo python3 tests/restore_test.py --actuator slice
sudo python3 tests/restore_test.py --actuator cgweight
```

**Pass:** each one ends with `ALL PASS`. Each run shows that the boost really changed the knob during the run, and that every value returned to its original while the workload was still running.

### T6: End-to-end smoke matrix (C)

```bash
tmux new -s fx
make quick                   # ~3 min: 8 runs + noise floor → metrics → figures
python3 tests/check_batch.py results/raw/quick_<id>
python3 analysis/validate.py --noise results/raw/quick_<id> --threads results/summary/quick_<id>_threads.csv
```

**Pass:**
- `make quick` ends with `done: 8 runs, 0 failed` and prints no `FAILED RUNS:` block.
- `check_batch.py` reports `0 FAIL`.
- Figures exist in `results/quick_figures/quick_<id>/`.
- `--threads` reports an aggregate error within ±5 % for both roles.

**Record:**
- The noise-floor line (wake-up lag p50/p99).
- The `--threads` errors.
- The p99 values in `results/summary/quick_<id>_cells.csv` for `default` and `waitboost-slice`.

### T7: Failure handling, interrupt and resume (B)

```bash
# terminal 1 (tmux)
sudo python3 harness/run_matrix.py harness/configs/quick.json --batch-id ft_test
# terminal 2, while the 3rd or 4th run is in progress
sudo pkill -f "wlgen batch"
```

**Pass:**
- That run is reported as `failed:wlgen batch rc=-15` and appears in `results/raw/ft_test/failed_runs.csv`.
- The remaining runs continue.

Then test interrupt and resume:

```bash
sudo python3 harness/run_matrix.py harness/configs/quick.json --batch-id ft_test2   # press Ctrl-C mid-way
ls /sys/fs/cgroup/fairexp 2>/dev/null || echo "cgroup removed"
pgrep -a 'wlgen|schedmon' || echo "no leftovers"
sudo python3 harness/run_matrix.py harness/configs/quick.json --resume results/raw/ft_test2
```

**Pass:**
- After Ctrl-C you see `cgroup removed` and `no leftovers`.
- The resume skips the runs that were already ok and completes the rest.

Delete `ft_test*` afterwards: `sudo rm -rf results/raw/ft_test*`.

### T8: Analysis pipeline on real quick data (C)

Copy the quick batch to Windows. In PowerShell, from the repo's `code` folder:

```powershell
scp -P 2222 -r fair@127.0.0.1:~/fairexp/code/results/raw/quick_<id> results/raw/
pip install numpy pandas scipy matplotlib        # once
python analysis/metrics.py results/raw/quick_<id>
python analysis/stats.py results/summary/quick_<id>_runs.csv
python analysis/plots.py --main quick_<id> --out results/quick_figures/quick_<id>
python analysis/make_tables.py --main quick_<id> --out results/quick_figures/quick_<id>/tables
```

**Pass:**
- No errors, and the figures open.
- Two numbers spot-checked in `quick_<id>_cells.csv` match the figures.
- `tables/tab_repro.tex` shows the VM's kernel and CPU.

### T9: Host noise gate (golden laptop only)

The host CPU mixes fast and slow core types, and Windows also runs the Hyper-V hypervisor. Both add timing noise, so check the noise before the golden run.

**Host preparation:**
- AC power, with the Windows power mode set to **Best performance**.
- Close browsers, IDEs and chat apps. Quit Docker Desktop and run `wsl --shutdown`.
- In Task Manager → Details → `VirtualBoxVM.exe`, set **priority High**.

**Measure** (inside the VM):

```bash
for i in 1 2 3 4 5; do ./build/wlgen calibrate --trials 9; done
make quick
python3 analysis/validate.py --noise results/raw/quick_<id>
```

**Gate:**
- The calibration values are within 5 % of each other.
- The noise verdict is `OK` (idle wake-up lag p99 < 1 ms).

**If the gate fails:**
1. Retry once with no host activity at all.
2. If it still fails, the laptop owner may turn the hypervisor off. Run `bcdedit /set hypervisorlaunchtype off` in an admin PowerShell and reboot (WSL2 and Docker stop working until you run `bcdedit /set hypervisorlaunchtype auto` and reboot again). Then measure again.

**Record:** the setting used for the golden run. It goes into the paper's threats-to-validity section either way.

### T10: Clean-room rebuild (D)

Build a **new** VM using only the documents (`code/setup/vm/README.md`, this file, `code/README.md`), then run T0–T3 and T6.

**Pass:** everything passes without help from anyone.

**Record:** time spent per step, plus every place the docs were unclear or wrong. Fix them in a PR.

### T11: Pilot, the whole study with short runs (C, then the golden-run owner)

```bash
tmux new -s pilot
make pilot                   # same as: sudo ./tests/pilot.sh
```

The pilot runs every config the golden run uses (tuning, main, held-out, ablation, scaling, overhead), with 4-second runs and the first 2 seeds, about 308 runs in total. It then runs trace validation, the batch audits, `tune.py`, all figures and all tables. Every stage prints PASS/FAIL, and it carries on after a failure so one pilot shows every problem.

**Pass:** the last line reads `PILOT: ALL STAGES PASS -> ready for the golden run`. Open the figures in `results/pilot_figures/<timestamp>/`: every figure should exist and look sensible.

**Two things the pilot does *not* tell you:**
- Its numbers are noisy (4 s runs, 2 seeds), and every figure carries a red "PILOT - NOT RESULTS" watermark. Never put pilot numbers in the paper.
- Do not pick policy parameters from the pilot. Tuning happens only in the golden run (§2, step 1).

**Run it twice:** first C runs it on their own VM (by Nov 4) to shake out bugs, then the golden-run owner runs it on the **golden machine** just before the golden run, to catch problems specific to that machine.

---

## 2. Phase 2: the golden run (designated laptop VM)

Start only when T0–T9 pass on the golden VM **and the pilot (T11) passes on the golden machine**. Do not use the laptop while runs are in progress.

### The one-command way: `make golden` (~9 h)

`make golden` runs Steps 1–4 below automatically:
1. Tuning.
2. Freezing the parameters. `tune.py --write` applies the pre-declared rule before any evaluation run starts, and keeps a copy in `results/summary/params_frozen_<TS>.json`.
3. All five evaluation batches, plus the trace validation.
4. The batch audits, the analysis, and the paper's figures and tables, written into `report/`.

It works the same way in WSL, in a VM, on native Linux and in the cloud.

**Before you start (Windows host):**
- Plug in the charger and set the power mode to **Best performance**.
- **Stop the laptop from sleeping**: Settings → System → Power → Screen and sleep → "When plugged in, put my device to sleep after" → **Never**. If Windows sleeps, WSL or the VM freezes and runs fail.
- **Pause Windows Update** for 1 day (Settings → Windows Update → Pause), so the laptop doesn't restart overnight.
- Close other apps.

**Run** (inside WSL or the VM):

```bash
cd ~/fairexp/code && git pull && tmux new -s golden
```
```bash
make golden
```

Enter your password once. Then detach with Ctrl+b, d. Come back later with `tmux attach -t golden`.

**At the end** it prints a `GOLDEN RUN SUMMARY` with PASS/FAIL per stage, and `GOLDEN RUN: ALL STAGES PASS`. The full log is in `results/summary/golden_<TS>.log`.

**If it was interrupted** (crash, reboot), resume with the timestamp shown at its start. Finished runs are kept, and the frozen parameters are not re-tuned:

```bash
make golden TS=<timestamp>
```

Afterwards, continue with Step 4 (publishing the raw data) and Phase 3. The steps below describe the same work done by hand.

### Step 1: Tuning (~25 min), then freeze parameters

```bash
git pull
make run CONFIG=tuning
make analyze BATCH=tuning_<ts>
python3 analysis/tune.py results/summary/tuning_<ts>_runs.csv
```

`tune.py` applies the pre-declared rule: the lowest interactive p99, provided batch throughput loses ≤ 3 %.
1. Paste its `by_actuator` block into `harness/configs/params_frozen.json`.
2. Update `_status` to say which batch it came from.
3. Commit, tag and push:

```bash
git add harness/configs/params_frozen.json
git commit -m "Freeze WaitBoost parameters from tuning_<ts>"
git tag params-frozen && git push && git push --tags
```

From here on, change no code or config. If a bug forces a change, fix it, re-tag, and **rerun every evaluation batch**.

### Step 2: Evaluation (~8 h, run overnight in tmux)

```bash
tmux new -s golden
for c in main heldout ablation scaling overhead; do make run CONFIG=$c; done; make validate-trace
```

| Config | Runs | Time |
|---|---|---|
| main | 285 + noise floor | ~3.1 h |
| heldout | 95 | ~1.0 h |
| ablation | 180 | ~2.0 h |
| scaling | 75 | ~0.8 h |
| overhead | 45 | ~0.5 h |

**If the run was interrupted** (crash, reboot), resume the affected batch:

```bash
sudo python3 harness/run_matrix.py harness/configs/<config>.json --resume results/raw/<batch> --pack
sudo chown -R $(id -u):$(id -g) results/raw
```

### Step 3: Audit every batch

```bash
for b in results/raw/*_2026*/; do python3 tests/check_batch.py "$b"; done
```

Every batch must report `0 FAIL`.

- **Failed runs** (crashes, timeouts) may be resumed **once** with `--resume`. Never rerun a run because its *result* looks bad. The paper reports both the failure count and the resume count.

### Step 4: Summaries and publishing the raw data

```bash
for b in results/raw/*_2026*/; do make analyze BATCH=$(basename "$b"); done
git add results/summary results/raw/*.tar.zst*
git commit -m "Golden run results <date>"
git push
```

- `make run` already packed each batch into `results/raw/<batch>.tar.zst`. It splits the archive into 90 MB `.partNN` files if needed, because GitHub rejects files over 100 MB, and writes a `.sha256`.
- To restore a split archive: `cat <batch>.tar.zst.part* > <batch>.tar.zst && tar --zstd -xf <batch>.tar.zst`.

## 3. Phase 3: figures, tables and consistency (C, then D)

Run on Windows after `git pull`, from `code/`:

```powershell
python analysis/plots.py --main <main> --heldout <heldout> --ablation <ablation> --overhead <overhead> --scaling <scaling>
python analysis/make_tables.py --main <main> --heldout <heldout> --overhead <overhead> --ablation <ablation>
python analysis/validate.py --trace results/raw/<validation> --threads results/summary/<main>_threads.csv --noise results/raw/<main>
```

This writes `report/figures/*.pdf` and `report/tables/*.tex` (including `numbers.tex`). Commit them and upload them to Overleaf.

**Consistency checklist (D, before submission):**
- [ ] Every number in the text comes from a `numbers.tex` macro or a generated table. Nothing is typed by hand.
- [ ] Failed and resumed runs are reported, with counts per batch.
- [ ] The validation numbers come from T4 and `--threads`, and the noise floor from `--noise`. All are stated in Results.
- [ ] The reproducibility table (`tab_repro.tex`) matches the golden VM, and the commit and tag are given in the reproducibility guide.
- [ ] The negative results ("where WaitBoost does not help") are in Results.
- [ ] `docs/Code_and_Reproducibility_Guide.pdf` matches the final run.

---

## 4. What "a result" is

Each paper claim compares a policy with **B0 on the same seed**, so the arrival schedule is identical.

| Claim | Metric | Source |
|---|---|---|
| H1: unfairness exists under B0 | SLO misses, unfairness episodes, p99 slowdown vs. the no-batch (x0) run | `tab_main.tex`, `timeline_h1.pdf`, `cdf_M*.pdf` |
| H2: WaitBoost improves p99 in at least one regime | p99 ratio and its 95 % CI entirely below 1 | `pareto_main.pdf`, `tab_main.tex` |
| H2: trade-off | Batch throughput ratio, Jain index, batch max wait | Same as above, plus `waits_M*_x4.pdf` |
| H3: slice beats nice on fairness | WB-slice vs WB-nice: similar p99 ratio, smaller fairness loss | `ablation.pdf`, `tab_main.tex` |
| Generalization | Held-out M4 | `pareto_heldout.pdf`, `tab_heldout.tex` |
| Measurement is trustworthy | Hand check, trace match, schedstat error, drops, overhead | T4, `validation.pdf`, `tab_overhead.tex` |

## 5. Bug report template (post in the team chat or as a GitHub issue)

```
Test:        T?  (e.g. T3 selftest)
VM:          output of `uname -r`, and `git -C ~/fairexp log -1 --oneline`
Command:     exact command
Expected:    …
Got:         paste output (last ~40 lines)
Files:       results/raw/<batch>/<run>/{meta.json, schedmon.stderr, wlgen_*.stderr}
```

## 6. Sign-off

| ID | Owner | Date | Result | Notes / recorded values |
|---|---|---|---|---|
| T0 | all | | | kernel: … sched_ext: … |
| T1 | all | | | |
| T2 | B | | | calibration: … |
| T3 | A | | | sleeper eBPF vs schedstat: … |
| T4 | A | | | matched …%, median diff … µs; hand check committed |
| T5 | B | | | nice / slice / cgweight |
| T6 | C | | | noise p99 …; schedstat error … |
| T7 | B | | | |
| T8 | C | | | |
| T9 | owner | | | hypervisor on/off; calibration spread … |
| T10 | D | | | doc fixes PR: … |
| T11 | C | | | pilot on own VM: all stages PASS? duration … |
| T11 | owner | | | pilot on golden machine: all stages PASS? |
