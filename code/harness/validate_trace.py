#!/usr/bin/env python3
"""Record one small run with BOTH schedmon and trace-cmd (raw ftrace), for validation (root).

  sudo python3 harness/validate_trace.py [--seconds 3]
  python3 analysis/validate.py --trace results/raw/validation_<timestamp>

Workload: one CPU, one batch spinner and one periodic interactive task (10 ms period,
1 ms work). Every wake-up of the interactive task must wait behind the spinner, which makes
the waits easy to check by hand. trace-cmd uses the monotonic clock (-C mono), the same
clock as bpf_ktime_get_ns(), so the two event streams can be matched by timestamp.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
sys.path.insert(0, str(HERE))
from cgroups import ExperimentCgroups  # noqa: E402
from run_experiment import _wait_ready  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--cpu", type=int, default=1)
    ap.add_argument("--out")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("validate_trace.py: needs root")
    out = Path(args.out) if args.out else CODE / "results" / "raw" / f"validation_{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    wlgen, schedmon = str(CODE / "build" / "wlgen"), str(CODE / "build" / "schedmon")
    os.sched_setaffinity(0, {0})

    cg = ExperimentCgroups()
    cg.setup(str(args.cpu))
    procs = []
    try:
        sm = subprocess.Popen([schedmon, "--cg-interactive", str(cg.path("interactive")), "--cg-batch",
                               str(cg.path("batch")), "--out", str(out), "--events", "--cpu", "0"],
                              stdout=subprocess.PIPE, stderr=open(out / "schedmon.stderr", "w"))
        procs.append(sm)
        _wait_ready(sm, 30)
        tc = subprocess.Popen(["trace-cmd", "record", "-C", "mono", "-e", "sched:sched_switch",
                               "-e", "sched:sched_wakeup", "-e", "sched:sched_wakeup_new",
                               "-M", str(1 << args.cpu), "-o", str(out / "trace.dat")],
                              stdout=open(out / "trace-cmd.stdout", "w"), stderr=subprocess.STDOUT)
        procs.append(tc)
        time.sleep(2.0)  # trace-cmd needs a moment to start recording
        start = time.monotonic_ns() + int(0.5e9)
        common = ["--seed", "1", "--start-at", str(start), "--warmup-s", "0.5", "--duration-s", str(args.seconds),
                  "--out", str(out)]
        b = subprocess.Popen([wlgen, "batch", "--threads", "1", "--unit-us", "1000", "--cgroup",
                              str(cg.path("batch"))] + common)
        i = subprocess.Popen([wlgen, "interactive", "--tasks", "1", "--arrival", "periodic", "--period-us", "10000",
                              "--work-us", "1000", "--cgroup", str(cg.path("interactive"))] + common)
        procs += [b, i]
        b.wait()
        i.wait()
        tc.send_signal(signal.SIGINT)
        tc.wait(timeout=60)
        sm.send_signal(signal.SIGTERM)
        sm.wait(timeout=30)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        cg.teardown()
    with open(out / "trace.txt", "w") as f:
        subprocess.run(["trace-cmd", "report", "-t", "-R", "-i", str(out / "trace.dat")], stdout=f, check=False)
    (out / "meta.json").write_text(json.dumps({"run_id": out.name, "kind": "validation", "cpu": args.cpu,
                                               "seconds": args.seconds, "status": "validation"}, indent=2))
    print("validation capture in", out)


if __name__ == "__main__":
    main()
