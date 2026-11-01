"""Per-run metrics: one row per run, plus per-thread validation rows.

Definitions (also used verbatim in the paper):
  response time      t_end - t_arrival of each measured interactive request (open loop)
  wake-up lag        t_start - t_arrival for requests that found their worker idle
  SLO violation      response time > slo_ms of the mix
  unfairness episode maximal run of consecutive 100 ms windows (by arrival) whose worst
                     response time exceeds the SLO
  run-queue wait     eBPF-measured time runnable-but-not-running (events inside the window)
  normalized share   received CPU / max-min fair entitlement (demand: interactive = its served
                     work, batch thread = one full core), Jain's index taken over all tasks
  temporal fairness  Jain's index across batch threads within each 100 ms window
  starved window     a (batch thread, 100 ms window) pair with zero completed units
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load import HIST_COLS, Run, runs_in  # noqa: E402

WIN_NS = 100_000_000
STARVE_NS = 100_000_000
CLASSES = {1: "inter", 2: "batch"}


def jain(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0 or not np.any(x):
        return np.nan
    return float(x.sum() ** 2 / (len(x) * np.square(x).sum()))


def maxmin_fair(demands, capacity):
    """Water-filling allocation: each task gets min(demand, equal share of what is left)."""
    d = np.asarray(demands, dtype=float)
    alloc = np.zeros_like(d)
    active = np.ones(len(d), dtype=bool)
    remaining = float(capacity)
    while active.any() and remaining > 1e-9:
        share = remaining / active.sum()
        sat = active & (d - alloc <= share)
        if sat.any():
            remaining -= float((d[sat] - alloc[sat]).sum())
            alloc[sat] = d[sat]
            active &= ~sat
        else:
            alloc[active] += share
            remaining = 0.0
    return alloc


def q(x, p):
    return float(np.quantile(x, p)) if len(x) else np.nan


def hist_quantile_us(hist, p):
    """Upper bound (µs) of the log2 bucket that contains quantile p."""
    h = np.asarray(hist, dtype=float)
    if h.sum() == 0:
        return np.nan
    k = int(np.searchsorted(np.cumsum(h), p * h.sum()))
    return float(2 ** k) if k > 0 else 1.0


def episodes(bad):
    """Count maximal runs of True and the longest one."""
    n, longest, cur = 0, 0, 0
    for b in bad:
        if b:
            cur += 1
            if cur == 1:
                n += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return n, longest


def interactive_metrics(r, m):
    rq = r.requests
    if rq is None or rq.empty:
        return np.array([])
    c = r.cfg
    t0, t1 = r.t_meas
    slo_ns = float(c["mix_params"].get("slo_ms", 10.0)) * 1e6
    rq = rq.sort_values(["task", "req"])
    prev_end = rq.groupby("task")["t_end_ns"].shift(1)
    meas = rq[rq.measured == 1]
    rt = (meas.t_end_ns - meas.t_arrival_ns).to_numpy(dtype=float)
    m.update(n_req=len(rt), rt_mean_us=rt.mean() / 1e3 if len(rt) else np.nan,
             rt_p50_us=q(rt, .5) / 1e3, rt_p95_us=q(rt, .95) / 1e3, rt_p99_us=q(rt, .99) / 1e3,
             rt_p999_us=q(rt, .999) / 1e3, rt_max_us=rt.max() / 1e3 if len(rt) else np.nan,
             slo_ms=slo_ns / 1e6, slo_viol=float((rt > slo_ns).mean()) if len(rt) else np.nan)

    idle = meas[(prev_end.loc[meas.index].isna()) | (meas.t_arrival_ns >= prev_end.loc[meas.index])]
    lag = (idle.t_start_ns - idle.t_arrival_ns).to_numpy(dtype=float)
    m.update(lag_p50_us=q(lag, .5) / 1e3, lag_p99_us=q(lag, .99) / 1e3)

    n_win = int((t1 - t0) // WIN_NS)
    win = ((meas.t_arrival_ns - t0) // WIN_NS).astype(int)
    worst = pd.Series(rt, index=meas.index).groupby(win).max()
    bad = np.zeros(n_win, dtype=bool)
    idx = worst.index[(worst.index >= 0) & (worst.index < n_win)]
    bad[idx] = worst.loc[idx].to_numpy() > slo_ns
    n_ep, longest = episodes(bad)
    m.update(episodes=n_ep, episode_longest_ms=longest * WIN_NS / 1e6, bad_window_frac=bad.mean() if n_win else np.nan)

    ipu = float(c["iters_per_us"])
    served = meas.groupby("task")["work_iters"].sum().to_numpy(dtype=float) / ipu * 1e3  # ns of CPU
    return served


def batch_metrics(r, m):
    b = r.batch
    dur = float(r.cfg["duration_s"])
    if b is None or b.empty:
        m.update(batch_tput=0.0)
        return np.array([])
    m.update(batch_tput=b.units.sum() / dur, batch_tput_per_thread=b.units.mean() / dur,
             jain_batch=jain(b.units))
    ts = r.batch_ts
    if ts is not None and not ts.empty:
        piv = ts.pivot_table(index="win", columns="thread", values="units", aggfunc="sum").fillna(0)
        per_win = piv.apply(jain, axis=1)
        tot = piv.sum(axis=1)
        m.update(jain_batch_win_mean=float(per_win.mean()), jain_batch_win_p5=float(per_win.quantile(.05)),
                 starved_win_frac=float((piv.to_numpy() == 0).mean()),
                 batch_tput_win_cv=float(tot.std() / tot.mean()) if tot.mean() > 0 else np.nan)
    return b.cpu_ns.to_numpy(dtype=float)


def share_metrics(m, served, batch_cpu, cores, dur_ns):
    cap = cores * dur_ns
    demand = np.concatenate([served, np.full(len(batch_cpu), dur_ns)])
    recv = np.concatenate([served, batch_cpu])
    if len(demand) == 0:
        return
    ent = maxmin_fair(demand, cap)
    with np.errstate(divide="ignore", invalid="ignore"):
        norm = np.where(ent > 0, recv / ent, np.nan)
    left = cap - served.sum()
    m.update(cpu_util=float(recv.sum() / cap), jain_norm_share=jain(norm),
             batch_norm_share=float(batch_cpu.sum() / left) if len(batch_cpu) and left > 0 else np.nan,
             inter_cpu_frac=float(served.sum() / cap))


def wait_metrics(r, m):
    ev = r.measured_events()
    tk = r.tasks
    for cls, name in CLASSES.items():
        if ev is not None and not ev.empty:
            e = ev[ev["class"] == cls]
            w = e.wait_ns.to_numpy(dtype=float)
            wk = e.wait_ns[e.kind != 1].to_numpy(dtype=float)
            wp = e.wait_ns[e.kind == 1].to_numpy(dtype=float)
            m.update({f"wait_{name}_n": len(w), f"wait_{name}_mean_us": w.mean() / 1e3 if len(w) else np.nan,
                      f"wait_{name}_p50_us": q(w, .5) / 1e3, f"wait_{name}_p99_us": q(w, .99) / 1e3,
                      f"wait_{name}_p999_us": q(w, .999) / 1e3,
                      f"wait_{name}_max_us": w.max() / 1e3 if len(w) else np.nan,
                      f"wait_{name}_wakeup_p99_us": q(wk, .99) / 1e3,
                      f"wait_{name}_preempt_p99_us": q(wp, .99) / 1e3,
                      f"starve_{name}_n": int((w > STARVE_NS).sum())})
        elif tk is not None and not tk.empty:  # no event stream: fall back to whole-run histograms
            t = tk[tk["class"] == cls]
            h = t[HIST_COLS].sum().to_numpy()
            n = t.n_wait.sum()
            m.update({f"wait_{name}_n": int(n),
                      f"wait_{name}_mean_us": t.wait_sum_ns.sum() / n / 1e3 if n else np.nan,
                      f"wait_{name}_p50_us": hist_quantile_us(h, .5), f"wait_{name}_p99_us": hist_quantile_us(h, .99),
                      f"wait_{name}_max_us": t.wait_max_ns.max() / 1e3 if len(t) else np.nan})
        if tk is not None and not tk.empty:
            t = tk[tk["class"] == cls]
            secs = max(r.stats.get("duration_ns", 0) / 1e9, 1e-9)
            m.update({f"csw_{name}_per_s": float((t.nvcsw.sum() + t.nivcsw.sum()) / secs),
                      f"invol_frac_{name}": float(t.nivcsw.sum() / max(t.nvcsw.sum() + t.nivcsw.sum(), 1)),
                      f"migr_{name}_per_s": float(t.migrations.sum() / secs)})


def collector_metrics(r, m):
    st = r.stats
    if not st:
        return
    em, dr = st.get("events_emitted", 0), st.get("events_dropped", 0)
    m.update(events_emitted=em, events_dropped=dr, drop_rate=dr / (em + dr) if em + dr else 0.0,
             actuator_errors=st.get("actuator_errors", 0), restore_errors=st.get("restore_errors", 0))
    for a, n in (st.get("actions") or {}).items():
        m[f"act_{a}"] = n
    run_cnt = run_ns = 0
    for p in st.get("bpf_progs", []):
        if p.get("run_cnt"):
            m[f"bpf_ns_{p['name']}"] = p["run_time_ns"] / p["run_cnt"]
            run_cnt += p["run_cnt"]
            run_ns += p["run_time_ns"]
    if run_cnt:
        m["bpf_ns_per_event"] = run_ns / run_cnt
        m["bpf_cpu_frac"] = run_ns / (st.get("duration_ns", 1) * r.cfg["cores"])


def validation_rows(r):
    """Per thread: eBPF total wait vs the kernel's schedstat run_delay (independent source)."""
    tk = r.tasks
    if tk is None or tk.empty:
        return []
    rows = []
    for role, summ in (("inter", r.isum), ("batch", r.bsum)):
        for th in summ.get("threads", []):
            hit = tk[tk.tid == th["tid"]]
            if hit.empty or not th.get("schedstat_delay_ns"):
                continue
            rows.append({"run_id": r.meta["run_id"], "policy": r.cfg["policy"], "role": role, "tid": th["tid"],
                         "ebpf_wait_ns": int(hit.wait_sum_ns.iloc[0]), "schedstat_delay_ns": th["schedstat_delay_ns"],
                         "ebpf_run_ns": int(hit.run_ns.iloc[0]), "schedstat_run_ns": th.get("schedstat_run_ns")})
    return rows


def run_metrics(r):
    c = r.cfg
    m = {"batch_id": r.meta["batch_id"], "run_id": r.meta["run_id"], "mix": c["mix"],
         "contention": c["contention"], "batch_per_core": c["batch_per_core"], "batch_threads": c["batch_threads"],
         "cores": c["cores"], "policy": c["policy"], "seed": c["seed"], "block": c.get("block"),
         "order": c.get("order"), "duration_s": c["duration_s"]}
    served = interactive_metrics(r, m)
    batch_cpu = batch_metrics(r, m)
    share_metrics(m, served, batch_cpu, c["cores"], float(c["duration_s"]) * 1e9)
    wait_metrics(r, m)
    collector_metrics(r, m)
    val = validation_rows(r)
    for role in ("inter", "batch"):
        e = sum(v["ebpf_wait_ns"] for v in val if v["role"] == role)
        s = sum(v["schedstat_delay_ns"] for v in val if v["role"] == role)
        if s:
            m[f"val_relerr_{role}"] = (e - s) / s
    return m, val


def summarize(batch_dir, out_dir):
    batch_dir, out_dir = Path(batch_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, vals = [], []
    runs = runs_in(batch_dir)
    for i, r in enumerate(runs):
        m, v = run_metrics(r)
        rows.append(m)
        vals += v
        if (i + 1) % 50 == 0:
            print(f"  {batch_dir.name}: {i + 1}/{len(runs)}", file=sys.stderr)
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / f"{batch_dir.name}_runs.csv", index=False)
    pd.DataFrame(vals).to_csv(out_dir / f"{batch_dir.name}_threads.csv", index=False)
    failed = batch_dir / "failed_runs.csv"
    if failed.exists():
        (out_dir / f"{batch_dir.name}_failed.csv").write_text(failed.read_text())
    return df


def main():
    ap = argparse.ArgumentParser(description="per-run metrics for one or more batch directories")
    ap.add_argument("batch_dirs", nargs="+")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "results" / "summary"))
    args = ap.parse_args()
    for b in args.batch_dirs:
        df = summarize(b, args.out)
        print(f"{b}: {len(df)} runs -> {args.out}")


if __name__ == "__main__":
    main()
