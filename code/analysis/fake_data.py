"""SYNTHETIC run directories in the real file formats, for testing the analysis pipeline.

  python analysis/fake_data.py [--out results/fake/fake_main] [--seeds 3]

Nothing produced here is a measurement. Every meta.json carries "synthetic": true, the
output lives under results/fake/ (git-ignored), and plots/tables built from it must never
reach the paper. The effect sizes are invented only so the plots have something to show.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CODE = Path(__file__).resolve().parents[1]
MIXES = {k: v for k, v in json.loads((CODE / "harness/configs/mixes.json").read_text()).items()
         if not k.startswith("_")}
POLICIES = ["default", "nice", "cgweight", "slice", "waitboost-nice", "waitboost-slice"]
DELAY_F = {"default": 1.0, "nice": .55, "cgweight": .65, "slice": .5, "waitboost-nice": .5, "waitboost-slice": .4}
TPUT_F = {"default": 1.0, "nice": .995, "cgweight": .99, "slice": .975, "waitboost-nice": .99, "waitboost-slice": .98}
CONT = {"x0": 0, "x1": 1, "x2": 2, "x4": 4}
CONT_F = {"x0": .02, "x1": .5, "x2": 1.0, "x4": 1.9}
IPU = 600.0
HIST = 27


def log2_hist(w_ns):
    us = np.asarray(w_ns) / 1e3
    b = np.where(us < 1, 0, np.floor(np.log2(np.maximum(us, 1))).astype(int) + 1)
    return np.bincount(np.minimum(b, HIST - 1), minlength=HIST)


def make_run(out, batch_id, mix, cont, pol, cores, seed, dur, warm, block, order):
    rng = np.random.default_rng(abs(hash((mix, cont, pol, cores, seed))) % 2**32)
    sched_rng = np.random.default_rng(seed * 1000 + list(MIXES).index(mix))  # same schedule across policies
    mp = MIXES[mix]
    it = mp["interactive"]
    n_b = CONT[cont] * cores
    t0 = 10_000_000_000
    tms, tme = t0 + int(warm * 1e9), t0 + int((warm + dur) * 1e9)
    run_id = f"{mix}_{cont}_{pol}_c{cores}_s{seed}"
    d = out / run_id
    d.mkdir(parents=True, exist_ok=True)

    base_delay = 1.5e6 * CONT_F[cont] * DELAY_F[pol] * (1 if pol != "default" or n_b == 0 else 1)
    rows, threads_i, ev = [], [], []
    rate = it.get("rate", 1e6 / it.get("period_us", 16667))
    for task in range(it["tasks"]):
        n = sched_rng.poisson(rate * (warm + dur))
        arr = np.sort(sched_rng.uniform(0, (warm + dur) * 1e9, n)).astype(np.int64)
        work_us = np.full(n, it["work_us"], dtype=float)
        if it.get("work_dist") == "lognormal":
            work_us *= np.exp(it.get("work_sigma", .5) * sched_rng.standard_normal(n))
        wait = rng.exponential(base_delay, n) * (1 + 4 * (rng.random(n) < 0.02))
        start = t0 + arr + (rng.exponential(30e3, n) + wait * .3).astype(np.int64)
        end = start + (work_us * 1e3 + wait * .7).astype(np.int64)
        tid = 1000 + task
        for k in range(n):
            rows.append((task, tid, k, t0 + arr[k], start[k], end[k], int(work_us[k] * IPU),
                         int(tms <= t0 + arr[k] < tme)))
        ev += [(int(e), tid, 1, 1, int(w), 0) for e, w in zip(start, wait)]
        threads_i.append({"idx": task, "tid": tid, "nice_readback": 0, "slice_readback_ns": 2800000,
                          "setup_errno": 0, "schedstat_run_ns": int(work_us.sum() * 1e3),
                          "schedstat_delay_ns": int(wait.sum() * (1 + rng.normal(0, .01))), "schedstat_slices": n})
    rq = pd.DataFrame(rows, columns=["task", "tid", "req", "t_arrival_ns", "t_start_ns", "t_end_ns", "work_iters",
                                     "measured"])
    rq.to_csv(d / "requests.csv", index=False)

    inter_util = it["tasks"] * rate * it["work_us"] / 1e6
    threads_b, brows, tsrows = [], [], []
    if n_b:
        per = max(cores - inter_util, 0.1) / n_b * TPUT_F[pol]
        for th in range(n_b):
            units = int(rng.normal(per * dur * 1000, per * dur * 10))
            brows.append((th, 2000 + th, units, int(per * dur * 1e9), int(dur * 1e9)))
            lam = per * 100 * np.ones(int(dur * 10))
            starve = rng.random(len(lam)) < (0.002 * CONT[cont] if pol in ("nice", "cgweight") else 0.0005)
            u = np.where(starve, 0, rng.poisson(lam))
            tsrows += [(th, int(warm * 10) + k, tms + k * 100_000_000, int(x)) for k, x in enumerate(u)]
            wb = rng.exponential(2.8e6 * CONT[cont] / 2, int(dur * 400))
            ev += [(int(tms + rng.uniform(0, dur * 1e9)), 2000 + th, 2, 2, int(w), 1) for w in wb]
            threads_b.append({"idx": th, "tid": 2000 + th, "nice_readback": 10 if pol == "nice" else 0,
                              "slice_readback_ns": 2800000, "setup_errno": 0,
                              "schedstat_run_ns": int(per * dur * 1e9),
                              "schedstat_delay_ns": int(wb.sum() * (1 + rng.normal(0, .01))), "schedstat_slices": len(wb)})
        pd.DataFrame(brows, columns=["thread", "tid", "units", "cpu_ns", "wall_ns"]).to_csv(d / "batch.csv", index=False)
        pd.DataFrame(tsrows, columns=["thread", "win", "t_win_start_ns", "units"]).to_csv(d / "batch_ts.csv", index=False)

    evdf = pd.DataFrame(ev, columns=["ts_ns", "tid", "class", "cpu", "wait_ns", "kind"]).sort_values("ts_ns")
    evdf.to_csv(d / "events.csv", index=False)
    trows = []
    for tid, g in evdf.groupby("tid"):
        cls = int(g["class"].iloc[0])
        h = log2_hist(g.wait_ns)
        trows.append([tid, 999, "wlgen", cls, len(g), len(g), int(g.wait_ns.sum()), int(g.wait_ns.max()),
                      int(dur * 1e9 * .3), len(g) if cls == 1 else len(g) // 10, 0 if cls == 1 else len(g),
                      int(rng.integers(0, 50))] + list(h))
    cols = ["tid", "tgid", "comm", "class", "n_wakeups", "n_wait", "wait_sum_ns", "wait_max_ns", "run_ns", "nvcsw",
            "nivcsw", "migrations"] + [f"h{i:02d}" for i in range(HIST)]
    pd.DataFrame(trows, columns=cols).to_csv(d / "tasks.csv", index=False)

    (d / "interactive_summary.json").write_text(json.dumps({"role": "interactive", "threads": threads_i}))
    if n_b:
        (d / "batch_summary.json").write_text(json.dumps({"role": "batch", "threads": threads_b}))
    stats = {"events_emitted": len(evdf), "events_dropped": 0, "duration_ns": int((warm + dur + 2) * 1e9),
             "actuator_errors": 0, "restore_errors": 0,
             "actions": {"boost": 40, "decay": 38, "guard": 0, "revoke": 1, "restore": 1} if "waitboost" in pol else {},
             "bpf_progs": [{"name": "on_switch", "run_cnt": len(evdf) * 3, "run_time_ns": len(evdf) * 3 * 420},
                           {"name": "on_wakeup", "run_cnt": len(evdf), "run_time_ns": len(evdf) * 310}]}
    (d / "schedmon_stats.json").write_text(json.dumps(stats))
    meta = {"synthetic": True, "run_id": run_id, "batch_id": batch_id, "status": "ok",
            "created": "SYNTHETIC", "t0_ns": t0, "t_meas_start_ns": tms, "t_meas_end_ns": tme,
            "config": {"mix": mix, "mix_params": mp, "contention": cont, "batch_per_core": CONT[cont],
                       "batch_threads": n_b, "interactive_tasks": it["tasks"], "cores": cores, "policy": pol,
                       "seed": seed, "warmup_s": warm, "duration_s": dur, "unit_us": 1000, "iters_per_us": IPU,
                       "block": block, "order": order}}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(CODE / "results" / "fake" / "fake_main"))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--policies", default=",".join(POLICIES))
    ap.add_argument("--mixes", default="M1,M2,M3")
    ap.add_argument("--conts", default="x1,x2,x4")
    ap.add_argument("--cores", default="2")
    ap.add_argument("--no-reference", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "SYNTHETIC_DATA_README.txt").write_text("Synthetic data from analysis/fake_data.py. NOT measurements.\n")
    pols = args.policies.split(",")
    rng = np.random.default_rng(7)
    for p in pols:  # invented effects for policies without a preset
        DELAY_F.setdefault(p, float(rng.uniform(.35, .8)))
        TPUT_F.setdefault(p, 1.0 if "nocollector" in p else float(rng.uniform(.96, 1.0)))
    n = 0
    for block, seed in enumerate(range(101, 101 + args.seeds)):
        order = 0
        for cores in map(int, args.cores.split(",")):
            for mix in args.mixes.split(","):
                for cont in args.conts.split(","):
                    for pol in pols:
                        make_run(out, out.name, mix, cont, pol, cores, seed, args.duration, 0.5, block, order)
                        order += 1
                        n += 1
                if not args.no_reference:
                    make_run(out, out.name, mix, "x0", "default", cores, seed, args.duration, 0.5, block, order)
                    n += 1
    print(f"wrote {n} synthetic runs to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
