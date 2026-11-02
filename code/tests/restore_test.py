#!/usr/bin/env python3
"""Safety test for WaitBoost: knobs really change during a run and are all restored (root).

  sudo python3 tests/restore_test.py [--actuator nice|slice|cgweight] [--cpu 1]

The test runs 2 interactive tasks and 4 batch threads on ONE CPU, so the interactive
tasks wait. It starts schedmon with WaitBoost and a deliberately low threshold
(theta = 100 µs) so that boosts happen, then:
  t = 6 s   samples the interactive threads' nice / slice, or the cgroup weight; expects a boost
  t = 8 s   stops schedmon with SIGTERM while the workload keeps running
  t = 9 s   samples again; expects every value back at its original
It also checks policy_log.csv (boost and restore rows) and schedmon_stats.json (restore_errors == 0).
"""
import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
sys.path.insert(0, str(CODE / "harness"))
from cgroups import ExperimentCgroups  # noqa: E402
from run_experiment import _wait_ready  # noqa: E402

SYS_sched_getattr = {"x86_64": 315, "aarch64": 275}[os.uname().machine]


class SchedAttr(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("policy", ctypes.c_uint32), ("flags", ctypes.c_uint64),
                ("nice", ctypes.c_int32), ("priority", ctypes.c_uint32), ("runtime", ctypes.c_uint64),
                ("deadline", ctypes.c_uint64), ("period", ctypes.c_uint64), ("util_min", ctypes.c_uint32),
                ("util_max", ctypes.c_uint32)]


libc = ctypes.CDLL(None, use_errno=True)


def fair_params(tid):
    a = SchedAttr()
    if libc.syscall(SYS_sched_getattr, tid, ctypes.byref(a), ctypes.sizeof(a), 0) != 0:
        return None
    return a.nice, a.runtime


def threads_of(pid):
    try:
        return sorted(int(t) for t in os.listdir(f"/proc/{pid}/task") if int(t) != pid)
    except FileNotFoundError:
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--actuator", default="nice", choices=["nice", "slice", "cgweight"])
    ap.add_argument("--cpu", type=int, default=1)
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("restore_test.py: needs root")
    out = CODE / "results" / "raw" / f"restoretest_{args.actuator}_{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    wlgen, schedmon = str(CODE / "build" / "wlgen"), str(CODE / "build" / "schedmon")
    os.sched_setaffinity(0, {0})
    cg = ExperimentCgroups()
    cg.setup(str(args.cpu))
    results, procs = [], []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")

    try:
        sm = subprocess.Popen([schedmon, "--cg-interactive", str(cg.path("interactive")), "--cg-batch",
                               str(cg.path("batch")), "--out", str(out), "--cpu", "0", "--policy", "waitboost",
                               "--actuator", args.actuator, "--theta-us", "100", "--min-sleep", "0.3"],
                              stdout=subprocess.PIPE, stderr=open(out / "schedmon.stderr", "w"))
        procs.append(sm)
        _wait_ready(sm, 30)
        start = time.monotonic_ns() + int(0.5e9)
        common = ["--seed", "1", "--start-at", str(start), "--warmup-s", "0", "--duration-s", "12",
                  "--out", str(out), "--iters-per-us", "500"]
        b = subprocess.Popen([wlgen, "batch", "--threads", "4", "--unit-us", "1000",
                              "--cgroup", str(cg.path("batch"))] + common)
        i = subprocess.Popen([wlgen, "interactive", "--tasks", "2", "--rate", "100", "--work-us", "300",
                              "--cgroup", str(cg.path("interactive"))] + common)
        procs += [b, i]
        time.sleep(1.0)
        tids = threads_of(i.pid)
        before = {t: fair_params(t) for t in tids}
        w_before = cg.weight("interactive")
        print(f"interactive tids {tids}; start values {before}; interactive cpu.weight {w_before}")
        check("interactive threads found and readable", bool(tids) and all(before.values()))
        if not results[-1]:
            raise SystemExit("cannot continue without the interactive threads")

        # Levels rise and decay every few tens of ms, so sample for 5 s and keep the strongest boost seen.
        min_nice = min_slice = None
        max_w = w_before
        deadline = time.time() + 5.0
        while time.time() < deadline:
            for t in tids:
                p = fair_params(t)
                if p:
                    min_nice = p[0] if min_nice is None else min(min_nice, p[0])
                    min_slice = p[1] if min_slice is None else min(min_slice, p[1])
            max_w = max(max_w, cg.weight("interactive") or 0)
            time.sleep(0.05)
        if args.actuator == "nice":
            check("boost seen during run (nice went below original)",
                  min_nice is not None and min_nice < min(v[0] for v in before.values()), f"lowest nice {min_nice}")
        elif args.actuator == "slice":
            check("boost seen during run (slice went below original)",
                  min_slice is not None and min_slice < min(v[1] for v in before.values()),
                  f"shortest slice {min_slice} ns")
        else:
            check("boost seen during run (cpu.weight raised)", max_w > w_before, f"{w_before} -> max {max_w}")

        sm.send_signal(signal.SIGTERM)
        rc = sm.wait(timeout=30)
        check("schedmon exited cleanly", rc == 0, f"rc={rc}")
        time.sleep(1.0)
        after = {t: fair_params(t) for t in tids}
        check("all nice/slice values restored while workload still runs", after == before, str(after))
        check("cgroup weight restored", cg.weight("interactive") == w_before, f"{cg.weight('interactive')}")
        check("workload still running at restore check", i.poll() is None and b.poll() is None)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        cg.teardown()

    log = (out / "policy_log.csv").read_text().splitlines() if (out / "policy_log.csv").exists() else []
    check("policy_log has boost rows", any(",boost," in line for line in log), f"({len(log) - 1} rows)")
    # A restore row is written only for threads still boosted at exit; check that every such thread got one.
    last_level, restored = {}, set()
    for line in log[1:]:
        f = line.split(",")  # t_ns,tid,class,action,level_from,level_to,...
        if f[3] == "restore":
            restored.add(f[1])
        else:
            last_level[f[1]] = int(f[5])
    still_boosted = {tid for tid, lvl in last_level.items() if lvl > 0}
    check("every thread boosted at exit has a restore row", still_boosted <= restored,
          f"(boosted at exit: {sorted(still_boosted)}, restored: {sorted(restored)})")
    st = json.loads((out / "schedmon_stats.json").read_text()) if (out / "schedmon_stats.json").exists() else {}
    check("restore_errors == 0", st.get("restore_errors") == 0, str(st.get("restore_errors")))
    check("actuator_errors == 0", st.get("actuator_errors") == 0, str(st.get("actuator_errors")))
    print(f"\nrestore_test ({args.actuator}): {'ALL PASS' if all(results) else 'FAILURES ABOVE'}   files: {out}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
