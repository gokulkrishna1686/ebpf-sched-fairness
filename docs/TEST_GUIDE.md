# Test Guide: how each test works, how long it takes, step by step

This is the tutorial. [`TESTING.md`](TESTING.md) is the checklist: who does what, the sign-off table and the golden run. Each test below has the same parts:

- **What it checks**: the question the test answers, in plain words.
- **How it works**: what happens behind the scenes.
- **Time**: how long you work on it, and how long the machine runs.
- **Steps**: commands to type, in order.
- **What you should see**: example output. Your numbers will differ.
- **Pass if** / **If it fails** / **Record**.

---

## Tests at a glance

| ID | Test | Who | Needs `sudo` | Your time | Machine time |
|---|---|---|---|---|---|
| — | Part 0: create the VM and get the code | everyone | yes | 30 min | ~30 min install |
| T0 | Environment check | everyone | yes | 5 min | 5–10 min (downloads) |
| T1 | Build | everyone | no | 2 min | < 1 min |
| T2 | Workload generator smoke test | B | no | 2 min | < 1 min |
| T3 | Collector selftest | A | yes | 5 min | ~5 s |
| T4 | Trace validation + hand check | A | yes | 30–40 min | ~30 s |
| T5 | Policy restore / safety (×3) | B | yes | 5 min | ~1 min |
| T6 | Quick end-to-end run (`make quick`) | C | yes | 10 min | ~3 min |
| T7 | Failure, interrupt and resume | B | yes | 15 min | ~5 min |
| T8 | Analysis on Windows | C | no | 20–30 min | ~2 min |
| T9 | Host noise gate | golden-run owner | yes | 15 min | ~5 min |
| T10 | Clean-room rebuild from the docs | D | yes | 1–1.5 h | mostly install |
| T11 | Pilot (whole study, short runs) | C, then the golden-run owner | yes | 10 min | ~1 h, unattended |

**Time per person:**

| Person | Tests | Time |
|---|---|---|
| A | T0, T1, T3, T4 | ~1 h |
| B | T0, T1, T2, T5, T7 | ~40 min |
| C | T0, T1, T6, T8, T11 | ~1 h of work plus ~1 h of waiting |
| D | T0, T1, T10 | ~1.5–2 h |
| Golden-run owner | T9, T11 | ~1.5 h, mostly waiting |

---

## The project in 2 minutes

Read this first. Without it the test outputs won't make sense.

- **The question.** On Linux, short "interactive" tasks have to wait for the CPU when long "batch" tasks keep it busy. We measure that waiting, and we test whether a small policy can reduce it without hurting the batch work.
- **wlgen** (`build/wlgen`) is the fake workload:
  - **Interactive tasks** wake up at random times (decided by a *seed*), do a short burst of work (about 1 ms) and go back to sleep. Each request's **response time** is recorded.
  - **Batch threads** compute non-stop and count how many work units they finish.
- **schedmon** (`build/schedmon`) is the measuring tool. It loads small **eBPF** programs into the kernel. They see every time the scheduler wakes a task up or switches tasks, so they can measure each **run-queue wait**: the time a task was ready to run but had no CPU.
- **WaitBoost** is our policy, built into schedmon. Every 10 ms it checks the waits. If an interactive task waits too long, schedmon gives it a temporary **boost** in one of three ways:
  - a better `nice` value;
  - a shorter EEVDF time slice;
  - a bigger cgroup `cpu.weight`.

  When the task stops waiting, the boost fades away again.
- **cgroups** are the two folders under `/sys/fs/cgroup/fairexp/` (`interactive` and `batch`). Our programs run inside them, which is how schedmon knows which task is which.
- **The harness** (`harness/run_matrix.py`) runs many short experiments one after another and writes each one to `results/raw/<batch>/<run>/`.
- **The analysis** (`analysis/*.py`) turns those folders into numbers, figures and tables.

---

## Basics you'll need

- **Where to type.** Use a terminal *inside the VM* (Ubuntu: press Ctrl+Alt+T). Unless a step says otherwise, start in the code folder:
  ```bash
  cd ~/fairexp/code
  ```
- **`sudo`** runs a command as administrator. It asks for your VM password, unless you set the VM up with `--vm`.
- **Saving output for reporting.** Add `2>&1 | tee ~/T3.log` to the end of a command. You still see the output, and it's also saved to `~/T3.log`, so you can send the file instead of a screenshot.
- **tmux** keeps long jobs alive if a window closes or SSH drops:
  - Start a session: `tmux new -s fx`
  - Detach: press Ctrl+b, then d
  - Come back: `tmux attach -t fx`
- **Reading results.** Every check prints `PASS`, `WARN` or `FAIL` at the start of the line. A `WARN` is information, not a failure.
- **Get the latest code** before testing: `cd ~/fairexp && git pull`.

---

## Part 0: Create the VM and get the code (everyone, once)

**Time:** about 30 minutes of clicking, plus the install.

1. Download **Ubuntu 26.04 LTS Desktop** (amd64) from ubuntu.com/download/desktop. Desktop is easier than Server, because you can open the figures in it.
2. In VirtualBox, click **New**:
   - Name `fairexp`, select the ISO, and tick **Skip Unattended Installation**.
   - Hardware: **6144 MB RAM** (4096 is OK if your laptop has 8 GB) and **4 CPUs**.
   - Disk: **30 GB**.

   Use 4 CPUs, not 2: CPU 0 is kept for the system and the experiments use CPUs 1–3.
3. Start the VM and install Ubuntu with the defaults ("Erase disk" only erases the *virtual* disk). Restart when it asks.
4. **Take a snapshot** now (Machine → Take Snapshot → name it `clean`). If anything breaks later, you can roll back.
5. Open a terminal in the VM and get the code:
   ```bash
   sudo apt update && sudo apt install -y git gh
   ```
   ```bash
   gh auth login
   ```
   Answer **GitHub.com → HTTPS → Yes** (authenticate Git) **→ Login with a web browser**, then follow the code it shows.
   ```bash
   gh repo clone gokulkrishna1686/ebpf-sched-fairness ~/fairexp
   ```

**If it fails:**
- **"repository not found":** you haven't accepted the GitHub collaborator invite yet. Check your email.
- **"Could not get lock /var/lib/dpkg/lock":** Ubuntu is installing updates in the background after the first boot. Wait 5–10 minutes and try again.

---

## T0: Environment check (everyone)

**What it checks:** that your VM has everything the project needs: a new enough Linux kernel, eBPF support, cgroups and the compilers.

**How it works:** `provision.sh` installs the packages (clang, libbpf, bpftool, trace-cmd, Python libraries and so on) and saves their exact versions to `/var/log/fairexp-packages.txt`. It then checks the kernel: version 6.12 or newer (needed for custom EEVDF slices), BTF type information (needed for our eBPF programs), scheduler statistics, and cgroup v2 with the `cpu` and `cpuset` controllers. With `--vm` it also prepares the VM for long runs: passwordless `sudo`, a pinned kernel version, and background updates switched off.

**Time:** 5 minutes of yours, 5–10 minutes of downloading.

**Steps:**
```bash
cd ~/fairexp && sudo ./code/setup/provision.sh --vm 2>&1 | tee ~/T0.log
```
```bash
cd ~/fairexp/code && make check-kernel
```

**What you should see** (at the end):
```
--- checks
ok    kernel 7.0.0-38-generic >= 6.12 (EEVDF custom slices)
ok    BTF at /sys/kernel/btf/vmlinux
ok    CONFIG_BPF_SYSCALL=y
ok    CONFIG_DEBUG_INFO_BTF=y
ok    CONFIG_SCHEDSTATS=y
ok    cgroup v2 with cpu+cpuset
ok    bpftool runs
ok    clang runs
info  sched_ext available: optional scx baseline possible
```

**Pass if:** every line starts with `ok`. The `info` line can go either way.

**If it fails:**
- **The kernel check fails:** you aren't on Ubuntu 26.04 (check with `lsb_release -a`). Make a 26.04 VM.
- **apt errors:** see "Could not get lock" in Part 0, and check that the VM has internet access.

**Record:** your kernel version (`uname -r`) and the `info sched_ext` line.

---

## T1: Build (everyone)

**What it checks:** that the C code and the eBPF program compile on your machine.

**How it works:** `make` compiles `wlgen` with gcc. For the eBPF side it:
1. exports your kernel's type information to `build/vmlinux.h`;
2. compiles `bpf/schedmon.bpf.c` with clang for the "bpf" target;
3. generates a C "skeleton" header with bpftool;
4. compiles `schedmon` against libbpf.

**Time:** 2 minutes.

**Steps:**
```bash
cd ~/fairexp/code && make clean && make 2>&1 | tee ~/T1.log
```

**What you should see:** about six command lines (`cc …`, `bpftool btf dump …`, `clang … -target bpf …`, `bpftool gen skeleton …`, `cc … schedmon`), with no `warning:` or `error:` lines. Then:
```bash
ls -l build/wlgen build/schedmon
```
lists both files.

**Pass if:** there are no warnings or errors, and both programs exist.

**If it fails:**
- **`bpftool: command not found` or `bpf/libbpf.h: No such file`:** T0 didn't finish. Run it again.
- **Anything else:** send `~/T1.log`.

**Record:** pass/fail.

---

## T2: Workload generator smoke test (B)

**What it checks:**
- `wlgen` produces the same random schedule for the same seed. Without that, policies can't be compared fairly.
- The kernel accepts a custom time slice.
- The `nice` setting really takes effect.
- The CPU time adds up.
- Batch threads share the CPU evenly.

**How it works:**
1. `tests/wlgen_smoke.sh` measures how fast this CPU runs our work loop (*calibration*) three times.
2. It runs 2 batch threads (at nice 10) and 2 interactive tasks (asking for a 0.1 ms slice) together on one CPU for 3 seconds.
3. It runs the interactive tasks again alone, with the same seed.
4. It compares the output files.

No `sudo` is needed.

**Time:** 2 minutes.

**Steps:**
```bash
cd ~/fairexp/code && ./tests/wlgen_smoke.sh 2>&1 | tee ~/T2.log
```

**What you should see:**
```
PASS  calibration stable (512.3 515.0 510.8 iters/us)
PASS  output requests.csv exists
PASS  output interactive_summary.json exists
PASS  output batch.csv exists
PASS  output batch_ts.csv exists
PASS  output batch_summary.json exists
PASS  same seed -> identical schedule hash
PASS  kernel accepted the 100 us EEVDF slice (slice_readback_ns=100000)
PASS  batch threads run at nice 10
PASS  one CPU fully used: batch 0.81 + interactive 0.20 = 1.01
INFO  interactive p99: contended 5.61 ms vs alone 3.05 ms
PASS  batch threads progress evenly (units [1793, 1795])

wlgen_smoke: ALL PASS
```

**Pass if:** the last line is `wlgen_smoke: ALL PASS`.

A `WARN calibration spread > 5%` line is allowed. It means your laptop moves the VM between fast and slow CPU cores, which only matters on the golden machine (T9).

**If it fails:**
- **The slice check fails:** your kernel is older than 6.12 (see T0).
- **"one CPU fully used" is far from 1.0:** your laptop is very busy. Close other programs and run it again.

**Record:** the three calibration numbers and the INFO line.

---

## T3: Collector selftest (A)

**What it checks:** that the kernel accepts our eBPF programs, and that their wait measurements agree with the kernel's own bookkeeping.

**How it works:** `schedmon --selftest` does the following:
1. Loads the eBPF programs and attaches them to the scheduler events.
2. Starts tracking its own cgroup.
3. Pins itself to one CPU.
4. Starts a "spinner" thread on the same CPU, which keeps the CPU busy.
5. Lets the main "sleeper" thread sleep and wake 200 times. Every wake-up has to wait for the spinner.

It then prints what eBPF measured next to `/proc/.../schedstat`. That file is the kernel's own total of run-queue waiting for the same thread, a completely independent source.

**Time:** 5 minutes, and the test itself takes a few seconds.

**Steps:**
```bash
cd ~/fairexp/code && make selftest 2>&1 | tee ~/T3.log
```

**What you should see:**
```
schedmon selftest: tracking cgroup /sys/fs/cgroup/user.slice/user-1000.slice/session-2.scope, pinned to CPU 1
  events emitted=1043 dropped=0
  tid 5021    sleeper  wakeups=212   waits=402   mean_wait=  431.2us max_wait= 2071.5us run=    1.9ms vcsw=201 ivcsw=201 | schedstat run_delay=  173.4ms ebpf=  173.3ms
  tid 5022    spinner  wakeups=1     waits=203   mean_wait= 1210.0us max_wait= 2800.0us run=  420.3ms vcsw=0 ivcsw=203 | schedstat run_delay=    0.0ms ebpf=  245.6ms
  (spinner schedstat reads 0 after it exits; compare the sleeper row)
selftest PASS
```

**Pass if:**
- The output ends with `selftest PASS`.
- `dropped=0`.
- The sleeper has at least 150 waits.
- On the **sleeper** row, `ebpf=` is within **5 %** of `schedstat run_delay=`, or within 0.5 ms when both are tiny.

Ignore the spinner's schedstat value: that thread has already exited, so the kernel file reads 0.

**If it fails:**
- **`failed to load BPF programs`:** run this and send the file. It contains the kernel's verifier log:
  ```bash
  sudo ./build/schedmon --selftest --verbose 2>&1 | tail -80 > ~/T3-verbose.log
  ```
- **`attach failed`:** your kernel isn't Ubuntu 26.04's (see T0).

**Record:** the sleeper's two totals, `ebpf=` and `schedstat run_delay=`.

---

## T4: Trace validation and hand check (A)

**What it checks:** whether our eBPF wait times are *correct*. They're compared against a second, standard tool, and checked once more by a human. The assignment explicitly asks for "a small hand-checked trace".

**How it works:**
1. `validate_trace.py` runs a tiny workload on **one CPU**: one batch thread that never stops, plus one interactive task that wakes every 10 ms for 1 ms of work.
2. **schedmon** (eBPF) and **trace-cmd** (Linux's standard tracer, ftrace) record the same 3 seconds at the same time, using the same clock.
3. `validate.py` re-computes every wait from trace-cmd's raw log using the same rules as our eBPF program, matches each one to the eBPF event, and reports the differences.
4. It writes 20 of them to `validation_hand.md`, with the line numbers in the raw log, so you can check them yourself.

**Time:** about 1 minute for the machine; 20–30 minutes for the hand check.

**Steps:**
1. Record and compare:
   ```bash
   cd ~/fairexp/code && make validate-trace
   ```
   It ends with `validation capture in results/raw/validation_<timestamp>`. Copy that name, then:
   ```bash
   python3 analysis/validate.py --trace results/raw/validation_<timestamp> 2>&1 | tee ~/T4.log
   ```
2. Open the hand-check table:
   ```bash
   cat results/raw/validation_<timestamp>/validation_hand.md
   ```
   Each row has a wait and two line numbers: "runnable @ line" (when the task became ready) and "switch-in @ line" (when it got the CPU).
3. For each of the 20 rows, print the two lines:
   ```bash
   sed -n '1234p;1240p' results/raw/validation_<timestamp>/trace.txt
   ```
   Use your row's two line numbers instead of 1234 and 1240. You'll see lines like:
   ```
   wlgen-5123 [001] 4521.123456789: sched_wakeup: comm=wlgen pid=5124 prio=120 target_cpu=001
   wlgen-5123 [001] 4521.123987654: sched_switch: prev_comm=wlgen prev_pid=5123 prev_prio=120 prev_state=0 next_comm=wlgen next_pid=5124 next_prio=120
   ```
4. Subtract the timestamps yourself: 4521.123987654 − 4521.123456789 = 0.000530865 s = **530.9 µs**. This must match the "trace wait" column (±1 µs) and be close to the "eBPF wait" column.

   On rows of kind `preempt`, the first line is a `sched_switch` where the task was *switched out* while still runnable (`prev_pid` = the task, `prev_state=0`). The rule is the same: switch-in time minus that time.
5. Save the table with your column added, and push it:
   ```bash
   cd ~/fairexp && cp code/results/raw/validation_<timestamp>/validation_hand.md docs/validation_hand_checked.md
   ```
   ```bash
   nano docs/validation_hand_checked.md
   ```
   In nano, add a column "by hand (µs)" and fill it in. Save with Ctrl+O, Enter; exit with Ctrl+X.
   ```bash
   git add docs/validation_hand_checked.md && git commit -m "T4 hand-checked trace" && git pull --rebase && git push
   ```

**What you should see** (step 1):
```
trace waits: 612, matched to eBPF: 612 (100.0%)
|diff| median 0.31 µs, p99 1.20 µs, max 2.85 µs
wrote .../validation_waits.csv and .../validation_hand.md
```

**Pass if:**
- At least 95 % of trace waits are matched.
- The median |diff| is ≤ 5 µs and the p99 ≤ 20 µs.
- All 20 hand values agree.

**If it fails:**
- **`trace-cmd: command not found`:** rerun T0.
- **Very few matches:** send `~/T4.log` and the `results/raw/validation_<timestamp>` folder name.

**Record:** match %, the median and p99 diff, and "hand check committed".

---

## T5: Policy restore / safety test (B)

**What it checks:**
- WaitBoost really changes the kernel settings while it runs.
- It puts **every** setting back when it stops, even while the workload is still running.

This is the assignment's safety requirement ("automatic return to the default scheduler"). The test runs once for each of the three boost types.

**How it works:**
1. `tests/restore_test.py` puts 2 interactive tasks and 4 batch threads on one CPU, so the interactive tasks have to wait.
2. It starts schedmon with WaitBoost and a deliberately low threshold, so boosts are guaranteed.
3. For 5 seconds it repeatedly reads the interactive tasks' nice value and slice (the same system call `chrt` uses), or the cgroup weight, and keeps the strongest boost it sees.
4. It stops schedmon while the workload keeps running, and reads everything again: all values must be back to their originals.
5. It checks schedmon's own log for errors.

**Time:** about 15 seconds per run, so under 1 minute for all three.

**Steps:**
```bash
cd ~/fairexp/code && sudo python3 tests/restore_test.py --actuator nice 2>&1 | tee ~/T5-nice.log
```
```bash
sudo python3 tests/restore_test.py --actuator slice 2>&1 | tee ~/T5-slice.log
```
```bash
sudo python3 tests/restore_test.py --actuator cgweight 2>&1 | tee ~/T5-cgweight.log
```

**What you should see** (nice):
```
interactive tids [6101, 6102]; start values {6101: (0, 2100000), 6102: (0, 2100000)}; interactive cpu.weight 100
PASS  boost seen during run (nice went below original) lowest nice -9
PASS  schedmon exited cleanly rc=0
PASS  all nice/slice values restored while workload still runs {6101: (0, 2100000), 6102: (0, 2100000)}
PASS  cgroup weight restored 100
PASS  workload still running at restore check
PASS  policy_log has boost rows (37 rows)
PASS  every thread boosted at exit has a restore row (boosted at exit: ['6101'], restored: ['6101'])
PASS  restore_errors == 0 0
PASS  actuator_errors == 0 0

restore_test (nice): ALL PASS   files: /home/you/fairexp/code/results/raw/restoretest_nice_...
```

The pairs like `(0, 2100000)` are (nice, slice in ns). For slice, expect "shortest slice" well below the original; for cgweight, expect "100 -> max 800" or similar.

**Pass if:** all three runs end with `ALL PASS`.

**If it fails:**
- **"boost seen" fails:** send the log. The workload may not have created enough waiting on your VM.
- **A "restored" check fails:** that's serious. Send the log and the `files:` folder.

Cleanup afterwards: `sudo rm -rf results/raw/restoretest_*`.

**Record:** pass/fail for each of the three.

---

## T6: Quick end-to-end run (C)

**What it checks:** the whole chain at small scale: real experiments, then data files, metrics, statistics and figures.

**How it works:** `make quick` does the following:
1. Runs the harness with `harness/configs/quick.json`: one workload mix (M1), one contention level (2 batch threads per core), and all 7 policies plus a no-batch reference run.
2. Runs a 3-second "noise floor" measurement first.
3. Each run lasts 6 seconds. For each run the harness sets up the cgroups and the policy, starts schedmon, starts both wlgen programs, and collects the files. It then checks that everything was restored.
4. Afterwards it computes metrics and statistics and draws the figures.

The figures carry a "TEST RUN – NOT RESULTS" watermark.

**Time:** about 3 minutes of machine time, plus about 5 minutes for you to check the results.

**Steps:**
```bash
cd ~/fairexp/code && make quick 2>&1 | tee ~/T6.log
```
At the end it prints `figures: results/quick_figures/quick_<id>`. Copy the `<id>`, then:
```bash
python3 tests/check_batch.py results/raw/quick_<id>
```
```bash
python3 analysis/validate.py --noise results/raw/quick_<id> --threads results/summary/quick_<id>_threads.csv
```
```bash
xdg-open results/quick_figures/quick_<id>/pareto_main.png
```

**What you should see:**
```
8 runs, ~0.02 h  ->  /home/you/fairexp/code/results/raw/quick_20261102-160000
noise floor: ok
[1/8] M1_x2_nice_c2_s1: ok  (ETA 1 min)
...
[8/8] M1_x2_cgweight_c2_s1: ok  (ETA 0 min)
done: 8 runs, 0 failed
...
quick_20261102-160000_runs.csv: 8 cells, 6 paired comparisons
  wrote results/quick_figures/quick_20261102-160000/cdf_M1.pdf
  ...
```
`check_batch.py` ends with `quick_<id>: 0 FAIL, 0 WARN`. `validate.py` prints the idle wake-up latency (`verdict: OK` or `TOO NOISY`) and one line per role with an `aggregate err=`.

**Pass if:**
- `done: 8 runs, 0 failed`.
- `check_batch` shows `0 FAIL`.
- Both `aggregate err=` values are within ±5 %.
- The figures open.

A `TOO NOISY` verdict on *your* VM is fine. It only matters on the golden machine (T9).

**If it fails:** send `~/T6.log`. For a failed run, also send `results/raw/quick_<id>/<run>/meta.json` and its `*.stderr` files.

**Record:** the noise line, the two `aggregate err` values, and the `rt_p99_us` for `default` and `waitboost-slice` from `results/summary/quick_<id>_cells.csv`.

---

## T7: Failure, interrupt and resume (B)

**What it checks:** that a long run survives problems without losing or hiding data.
- A crashed experiment is recorded as failed, and the rest continue.
- Ctrl-C cleans up (no leftover cgroups or processes).
- `--resume` finishes only what's missing.

**How it works:** you deliberately kill one workload process in the middle of a run, then interrupt a second batch with Ctrl-C and resume it.

**Time:** about 15 minutes.

**Steps:**
1. Open two terminals in the VM. In **terminal 1**:
   ```bash
   cd ~/fairexp/code && sudo python3 harness/run_matrix.py harness/configs/quick.json --batch-id ft_test
   ```
2. When it prints `[2/8] …`, in **terminal 2**:
   ```bash
   sudo pkill -f "wlgen batch"
   ```
3. Back in terminal 1, one run shows `failed:wlgen batch rc=-15` and the others continue. Check the record:
   ```bash
   cat results/raw/ft_test/failed_runs.csv
   ```
4. Interrupt test. In terminal 1:
   ```bash
   sudo python3 harness/run_matrix.py harness/configs/quick.json --batch-id ft_test2
   ```
   After 2–3 runs, press **Ctrl-C**. You should see `interrupted; resume with --resume …`. Then check:
   ```bash
   ls /sys/fs/cgroup/fairexp 2>/dev/null || echo "cgroup removed"
   ```
   ```bash
   pgrep -a 'wlgen|schedmon' || echo "no leftovers"
   ```
5. Resume:
   ```bash
   sudo python3 harness/run_matrix.py harness/configs/quick.json --resume results/raw/ft_test2
   ```
   It should run only the missing runs and end with `done: 8 runs, 0 failed`.
6. Clean up:
   ```bash
   sudo rm -rf results/raw/ft_test*
   ```

**Pass if:**
- The killed run is in `failed_runs.csv` and the other runs completed.
- After Ctrl-C you see `cgroup removed` and `no leftovers`.
- The resume completes the batch.

**Record:** pass/fail for each of the three parts.

---

## T8: Analysis on Windows (C)

**What it checks:** that the analysis, which C will run on Windows for the paper, works with *real* data. It also checks that the numbers in the tables match the figures.

**How it works:**
1. You copy the quick batch out of the VM.
2. On Windows, you run the same four scripts the paper uses:
   - `metrics.py`: per-run numbers;
   - `stats.py`: medians, confidence intervals and paired comparisons;
   - `plots.py`: figures;
   - `make_tables.py`: LaTeX tables.

**Time:** 20–30 minutes the first time, mostly setup.

**Steps:**
1. **Let Windows reach the VM over SSH.** This is a one-time setup.
   1. With the VM running, open VirtualBox → VM Settings → Network → Adapter 1 → Advanced → **Port Forwarding**.
   2. Add a rule: Host IP `127.0.0.1`, Host Port `2222`, Guest Port `22`.
   3. In the VM, install the SSH server:
      ```bash
      sudo apt install -y openssh-server
      ```
2. **Prepare Windows:** install Python 3.10+ from python.org (tick "Add to PATH"), then in PowerShell:
   ```powershell
   pip install numpy pandas scipy matplotlib
   ```
   ```powershell
   gh repo clone gokulkrishna1686/ebpf-sched-fairness; cd ebpf-sched-fairness\code
   ```
   If you don't have `gh` on Windows, GitHub Desktop or `git clone` also works.
3. **Copy the batch from the VM** (use your VM username):
   ```powershell
   scp -P 2222 -r <vm-user>@127.0.0.1:~/fairexp/code/results/raw/quick_<id> results/raw/
   ```
4. **Run the analysis:**
   ```powershell
   python analysis/metrics.py results/raw/quick_<id>
   python analysis/stats.py results/summary/quick_<id>_runs.csv
   python analysis/plots.py --main quick_<id> --out results/quick_figures/quick_<id>
   python analysis/make_tables.py --main quick_<id> --out results/quick_figures/quick_<id>/tables
   ```
5. **Spot-check:**
   1. Open `results/summary/quick_<id>_paired.csv` in Excel and find the row `policy = waitboost-slice`. Read `rt_p99_us_ratio` (e.g. 0.62).
   2. Open `pareto_main.png`. The **WB-s** point must sit at that height on the y-axis.
   3. Do the same for one more policy.
   4. Open `tables/tab_repro.tex`: it must show the **VM's** kernel and CPU.

**Pass if:**
- All four scripts run without errors.
- Both spot-checks match.
- `tab_repro.tex` shows the VM.

**Record:** pass/fail, and anything confusing.

---

## T9: Host noise gate (golden-run owner, on the golden machine)

**What it checks:** whether the machine for the final results is quiet enough. If the host keeps pausing the VM or changing its speed, the measurements get blurry.

**How it works:** you prepare the host, then measure two things:
- **Calibration spread:** does CPU speed stay constant?
- **Idle wake-up latency** (the noise floor): how late does a task wake up when nothing else is running?

**Time:** about 15 minutes.

**Steps:**
1. **Prepare the host** (Windows laptop):
   - AC power, with the power mode set to **Best performance**.
   - Close other apps, quit Docker Desktop, and run `wsl --shutdown`.
2. **Pin the VM to the fast cores** (PowerShell, after the VM has started; repeat after every VM start):
   ```powershell
   $p = Get-Process VirtualBoxVM; $p.ProcessorAffinity = 0xFC03; $p.PriorityClass = 'High'
   ```
   `0xFC03` is the fast-core list for **Gokul's** laptop (logical CPUs 0, 1 and 10–15). Another laptop needs its own value. Skip this step on a native Linux machine or a cloud server.
3. **Measure** (in the VM):
   ```bash
   cd ~/fairexp/code && for i in 1 2 3 4 5; do ./build/wlgen calibrate --trials 9; done
   ```
   ```bash
   make quick
   ```
   ```bash
   python3 analysis/validate.py --noise results/raw/quick_<id>
   ```

**Pass if:**
- The five calibration values are within 5 % of each other.
- The noise check says `verdict: OK`, meaning the idle wake-up p99 is under 1 ms.

**If it fails:**
1. Retry once with the laptop completely idle.
2. If it still fails, the laptop owner may switch off the Windows hypervisor (see `code/setup/vm/README.md` §7). Or use a quieter machine instead: a native Linux PC, or a dedicated-CPU cloud server.

**Record:** the calibration values, the noise p99, and which host setup was used. This goes into the paper.

---

## T10: Clean-room rebuild (D)

**What it checks:** that someone who has never seen the project can set it up from our documents alone. The assignment requires a reproducibility document, and this test proves ours works.

**How it works:** you build a brand-new VM and follow **only** Part 0, T0, T1, T3 and T6 of this guide, plus `code/README.md`. You don't ask anyone. Note every place where you got stuck.

**Time:** 1–1.5 hours.

**Steps:**
1. Create a second VM from scratch (Part 0), or restore your `clean` snapshot.
2. Follow Part 0 → T0 → T1 → T3 → T6 exactly as written. Time each step.
3. Write down every unclear sentence, missing step or wrong command.
4. Fix the docs yourself and push, or send the list to the team.

**Pass if:** you reach a passing T6 without outside help.

**Record:** the time per step and the list of doc problems.

---

## T11: Pilot, the whole study with short runs (C, then the golden-run owner)

**What it checks:** that **every** part of the real study works end to end before we spend about 9 hours on it:
- all six experiment configs;
- trace validation;
- the batch audits;
- the parameter tuning step;
- all figures and tables.

**How it works:** `tests/pilot.sh` runs the same configs as the golden run, with **4-second runs and 2 seeds** instead of 30 seconds and 5. That's about 308 runs. It then runs every analysis step. Each stage prints PASS/FAIL, and it keeps going after a failure so you see every problem in one go. Everything is watermarked "PILOT – NOT RESULTS".

**Time:** 10 minutes of yours, about 1 hour of machine time (unattended).

**Steps:**
```bash
cd ~/fairexp/code && tmux new -s pilot
```
```bash
make pilot 2>&1 | tee ~/T11.log
```
Detach with Ctrl+b then d, and come back an hour later with `tmux attach -t pilot`.

**What you should see** (at the end):
```
=== PILOT SUMMARY (20261102-170000)
PASS  run tuning
PASS  run main
PASS  run heldout
PASS  run ablation
PASS  run scaling
PASS  run overhead
PASS  validate-trace
PASS  audit tuning
...
PASS  tune.py
PASS  figures
PASS  tables
PASS  validation
figures: results/pilot_figures/20261102-170000    batches: results/raw/pilot_*_20261102-170000
PILOT: ALL STAGES PASS -> ready for the golden run
```
Then open a few figures in `results/pilot_figures/<timestamp>/`. They should all exist and look sensible: lines and points present, nothing empty.

**Pass if:** the last line reads `ALL STAGES PASS`.

**If it fails:** the summary tells you which stage failed. Scroll up in `~/T11.log` to the `=== <stage name>` heading for the details, and send that part.

`run scaling` failing usually means the VM has fewer than 4 CPUs.

**Record:** pass/fail, and how long it took (compare the first and last `=== … (HH:MM:SS)` times).

---

## When something fails

Post this in the team chat or as a GitHub issue:

```
Test:      T?  (e.g. T3 selftest)
VM:        output of `uname -r`, and `git -C ~/fairexp log -1 --oneline`
Command:   the exact command
Expected:  …
Got:       the last ~40 lines of output, or attach the ~/T?.log file
Files:     results/raw/<batch>/<run>/{meta.json, schedmon.stderr, wlgen_*.stderr} if a run failed
```

Then fill in your row in the sign-off table at the end of [`TESTING.md`](TESTING.md).
