"""Validation of the eBPF wait measurements against two independent sources.

  python analysis/validate.py --trace results/raw/validation_<ts>   # raw ftrace (trace-cmd)
  python analysis/validate.py --threads results/summary/main_..._threads.csv  # kernel schedstat

--trace recomputes every run-queue wait of the traced threads from raw sched_wakeup and
sched_switch records, using the same rules as schedmon.bpf.c. It then matches each wait to
the eBPF event with the same tid whose switch-in time is within 50 µs, and writes:
  validation_waits.csv   every matched pair
  validation_hand.md     the first 20 pairs, with the raw trace lines, for the hand check
--threads compares each thread's total eBPF wait with the kernel's own schedstat run_delay.
"""
import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

LINE = re.compile(r"\s(\d+\.\d+):\s+(sched_switch|sched_wakeup_new|sched_wakeup):\s+(.*)$")
FIELD = re.compile(r"(\w+)=(\S+)")
TASK_REPORT_MAX = 0x100


def runnable_state(s):
    """prev_state of a still-runnable (preempted) task: 0, or the TASK_REPORT_MAX preempt marker."""
    try:
        v = int(s, 0)
        return v == 0 or v & TASK_REPORT_MAX
    except ValueError:
        return s.startswith("R")


def trace_waits(trace_txt, tids):
    runnable, out = {}, []
    with open(trace_txt, errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            m = LINE.search(line)
            if not m:
                continue
            ts = int(round(float(m.group(1)) * 1e9))
            ev, fields = m.group(2), dict(FIELD.findall(m.group(3)))
            if ev in ("sched_wakeup", "sched_wakeup_new"):
                pid = int(fields.get("pid", -1))
                if pid in tids and runnable.get(pid) is None:
                    runnable[pid] = (ts, lineno, "new" if ev.endswith("new") else "wakeup")
            else:
                prev, nxt = int(fields.get("prev_pid", -1)), int(fields.get("next_pid", -1))
                if prev in tids:
                    runnable[prev] = (ts, lineno, "preempt") if runnable_state(fields.get("prev_state", "1")) else None
                if nxt in tids and runnable.get(nxt):
                    t0, l0, kind = runnable[nxt]
                    out.append({"tid": nxt, "ts_ns": ts, "trace_wait_ns": ts - t0, "kind": kind,
                                "line_runnable": l0, "line_switch_in": lineno})
                    runnable[nxt] = None
    return pd.DataFrame(out)


def check_trace(d):
    d = Path(d)
    tids = set()
    for name in ("interactive_summary.json", "batch_summary.json"):
        p = d / name
        if p.exists():
            tids |= {t["tid"] for t in json.loads(p.read_text())["threads"]}
    tw = trace_waits(d / "trace.txt", tids)
    ev = pd.read_csv(d / "events.csv")
    ev = ev[ev.tid.isin(tids)].sort_values("ts_ns")
    rows = []
    for tid, g in tw.groupby("tid"):
        e = ev[ev.tid == tid]
        if e.empty:
            continue
        idx = np.searchsorted(e.ts_ns.to_numpy(), g.ts_ns.to_numpy())
        for (_, r), k in zip(g.iterrows(), idx):
            best = None
            for j in (k - 1, k):
                if 0 <= j < len(e):
                    dt = abs(int(e.ts_ns.iloc[j]) - r.ts_ns)
                    if dt < 50_000 and (best is None or dt < best[0]):
                        best = (dt, j)
            if best:
                rows.append({**r.to_dict(), "ebpf_wait_ns": int(e.wait_ns.iloc[best[1]]),
                             "ts_offset_ns": best[0]})
    m = pd.DataFrame(rows)
    if m.empty:
        print("no matched waits: check that trace.txt and events.csv cover the same threads")
        return
    m["diff_ns"] = m.ebpf_wait_ns - m.trace_wait_ns
    m.to_csv(d / "validation_waits.csv", index=False)
    lines = open(d / "trace.txt", errors="replace").read().splitlines()
    md = ["| # | tid | kind | trace wait (µs) | eBPF wait (µs) | diff (µs) | runnable @ line | switch-in @ line |",
          "|---|---|---|---|---|---|---|---|"]
    for i, r in m.head(20).reset_index(drop=True).iterrows():
        md.append(f"| {i + 1} | {r.tid} | {r.kind} | {r.trace_wait_ns / 1e3:.1f} | {r.ebpf_wait_ns / 1e3:.1f} | "
                  f"{r.diff_ns / 1e3:+.1f} | {r.line_runnable} | {r.line_switch_in} |")
    md += ["", "Raw lines for the first 5 pairs:", "```"]
    for _, r in m.head(5).iterrows():
        md += [lines[r.line_runnable - 1].strip(), lines[r.line_switch_in - 1].strip(), ""]
    md.append("```")
    (d / "validation_hand.md").write_text("\n".join(md) + "\n")
    n_trace = len(tw)
    print(f"trace waits: {n_trace}, matched to eBPF: {len(m)} ({len(m) / max(n_trace, 1):.1%})")
    print(f"|diff| median {m.diff_ns.abs().median() / 1e3:.2f} µs, p99 {m.diff_ns.abs().quantile(.99) / 1e3:.2f} µs, "
          f"max {m.diff_ns.abs().max() / 1e3:.2f} µs")
    print(f"wrote {d / 'validation_waits.csv'} and {d / 'validation_hand.md'}")


def check_threads(path):
    t = pd.read_csv(path)
    if t.empty:
        print("no thread rows")
        return
    t["rel_err"] = (t.ebpf_wait_ns - t.schedstat_delay_ns) / t.schedstat_delay_ns
    for role, g in t.groupby("role"):
        agg = (g.ebpf_wait_ns.sum() - g.schedstat_delay_ns.sum()) / g.schedstat_delay_ns.sum()
        print(f"{role:6s} threads={len(g):5d} aggregate err={agg:+.3%} per-thread |err| median="
              f"{g.rel_err.abs().median():.3%} p95={g.rel_err.abs().quantile(.95):.3%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trace")
    ap.add_argument("--threads")
    args = ap.parse_args()
    if args.trace:
        check_trace(args.trace)
    if args.threads:
        check_threads(args.threads)


if __name__ == "__main__":
    main()
