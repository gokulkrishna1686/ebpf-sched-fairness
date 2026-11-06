"""Pick WaitBoost's threshold θ from the tuning batch with a rule fixed in advance.

  python analysis/tune.py results/summary/tuning_<ts>_runs.csv [--min-tput 0.97]

Rule (declared before any evaluation run):
  1. Pair every tuning run with the B0 run of the same (mix, seed). Same seed means the
     same arrival schedule.
  2. For each actuator and θ, take the median over all pairs of
     interactive p99 / B0 and batch throughput / B0.
  3. Keep the θ values whose median throughput ratio is >= --min-tput (default 0.97, a
     throughput loss of at most 3%).
  4. Among those, pick the lowest median p99 ratio. If two are within 0.01, prefer the
     larger θ (it intervenes less). If none qualifies, pick the θ with the highest
     throughput ratio and say so.
The output is the "by_actuator" block for harness/configs/params_frozen.json.
"""
import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd

KEY = ["batch_id", "mix", "contention", "cores", "seed"]
PAT = re.compile(r"^wb([ns])-t(\d+)$")
FROZEN = Path(__file__).resolve().parents[1] / "harness" / "configs" / "params_frozen.json"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs_csv")
    ap.add_argument("--min-tput", type=float, default=0.97)
    ap.add_argument("--write", action="store_true", help="store the choice in params_frozen.json")
    ap.add_argument("--params", default=str(FROZEN), help="params file to update with --write")
    args = ap.parse_args()

    runs = pd.read_csv(args.runs_csv)
    base = runs[runs.policy == "default"].set_index(KEY)[["rt_p99_us", "batch_tput"]]
    rows = []
    for _, r in runs.iterrows():
        m = PAT.match(str(r.policy))
        if not m:
            continue
        k = tuple(r[c] for c in KEY)
        if k not in base.index:
            continue
        b = base.loc[k]
        rows.append({"actuator": {"n": "nice", "s": "slice"}[m.group(1)], "theta_us": int(m.group(2)),
                     "mix": r.mix, "seed": r.seed, "p99_ratio": r.rt_p99_us / b.rt_p99_us,
                     "tput_ratio": r.batch_tput / b.batch_tput})
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("no wbn-t*/wbs-t* runs paired with default found")

    summary = (df.groupby(["actuator", "theta_us"])
                 .agg(p99_ratio=("p99_ratio", "median"), tput_ratio=("tput_ratio", "median"), pairs=("p99_ratio", "size"))
                 .reset_index())
    per_mix = df.pivot_table(index=["actuator", "theta_us"], columns="mix", values="p99_ratio", aggfunc="median")
    print("Median over (mix, seed) pairs, ratios vs B0:")
    print(summary.merge(per_mix.add_prefix("p99_").reset_index(), on=["actuator", "theta_us"])
                 .round(3).to_string(index=False))

    chosen = {}
    for act, g in summary.groupby("actuator"):
        ok = g[g.tput_ratio >= args.min_tput]
        if ok.empty:
            pick = g.sort_values("tput_ratio", ascending=False).iloc[0]
            note = f"no θ keeps throughput >= {args.min_tput}; took the highest-throughput θ"
        else:
            best = ok.p99_ratio.min()
            pick = ok[ok.p99_ratio <= best + 0.01].sort_values("theta_us").iloc[-1]
            note = "rule applied"
        chosen[act] = {"theta_us": int(pick.theta_us)}
        print(f"\n{act}: θ = {int(pick.theta_us)} µs  (p99 ratio {pick.p99_ratio:.3f}, "
              f"throughput ratio {pick.tput_ratio:.3f}, {int(pick.pairs)} pairs) — {note}")
    if not args.write:
        print("\nPut this into harness/configs/params_frozen.json (or rerun with --write):")
        print(json.dumps({"by_actuator": chosen}, indent=2))
        return
    path = Path(args.params)
    data = json.loads(path.read_text())
    data["by_actuator"] = chosen
    data["_status"] = (f"FROZEN {time.strftime('%Y-%m-%d %H:%M')} by analysis/tune.py --write from tuning batch "
                       f"{runs.batch_id.iloc[0]} (rule: lowest median p99 ratio among thetas whose median "
                       f"throughput ratio is >= {args.min_tput}). Written before any evaluation batch ran.")
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\nfrozen parameters written to {path}: {json.dumps(chosen)}")


if __name__ == "__main__":
    main()
