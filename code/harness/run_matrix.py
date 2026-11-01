#!/usr/bin/env python3
"""Expand an experiment config into runs and execute them (needs root).

  sudo python3 harness/run_matrix.py harness/configs/main.json
  sudo python3 harness/run_matrix.py CONFIG --resume results/raw/main_20261105-220000
  python3 harness/run_matrix.py CONFIG --dry-run        # list runs and the time estimate

Runs are grouped into blocks by seed. Each block holds every (cores, mix, contention,
policy) combination in a shuffled order. The shuffle is fixed by "order_seed", so slow
drift of the host spreads over all policies instead of biasing one of them. Failed runs
are recorded, never dropped: see failed_runs.csv in the batch directory.
"""
import argparse
import csv
import importlib.util
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
sys.path.insert(0, str(HERE))

import baselines  # noqa: E402
from cgroups import ExperimentCgroups, isolate_housekeeping, release_housekeeping  # noqa: E402
from run_experiment import cpu_set, run_one  # noqa: E402

DEFAULTS = {
    "cores": [2], "first_cpu": 1, "housekeeping_cpu": 0, "warmup_s": 5, "duration_s": 30,
    "unit_us": 1000, "events": True, "bpf_stats": False, "reference": False,
    "order_seed": 20261101, "isolate_housekeeping": False, "noise_floor": None,
    "policies_custom": [], "settle_s": 1.0,
}


def load_config(path):
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(Path(path).read_text()))
    if isinstance(cfg.get("mixes"), str):  # shared mix definitions, e.g. "mixes.json"
        cfg["mixes"] = json.loads((Path(path).resolve().parent / cfg["mixes"]).read_text())
        cfg["mixes"] = {k: v for k, v in cfg["mixes"].items() if not k.startswith("_")}
    baselines.register(cfg.get("policies_custom"))
    return cfg


def run_id(mix, cont, policy, cores, seed):
    return f"{mix}_{cont}_{policy}_c{cores}_s{seed}"


def expand(cfg, batch_id):
    mixes = cfg["mixes"]
    use = cfg.get("use_mixes", list(mixes))
    combos = list(itertools.product(cfg["cores"], use, cfg["contention"].items(), cfg["policies"]))
    if cfg["reference"]:
        combos += [(c, m, ("x0", 0), "default") for c in cfg["cores"] for m in use]
    runs = []
    for block, seed in enumerate(cfg["seeds"]):
        order = list(combos)
        random.Random(cfg["order_seed"] * 1000 + seed).shuffle(order)
        for i, (cores, mix, (cont, per_core), policy) in enumerate(order):
            runs.append({
                "run_id": run_id(mix, cont, policy, cores, seed), "batch_id": batch_id,
                "mix_name": mix, "mix": mixes[mix], "contention": cont, "batch_per_core": per_core,
                "policy": policy, "cores": cores, "seed": seed, "warmup_s": cfg["warmup_s"],
                "duration_s": cfg["duration_s"], "unit_us": cfg["unit_us"], "events": cfg["events"],
                "bpf_stats": cfg["bpf_stats"], "block": block, "order": i,
            })
    return runs


def git_info():
    def git(*args):
        try:
            out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(CODE), *args],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": git("rev-parse", "HEAD"), "describe": git("describe", "--always", "--tags", "--dirty"),
            "dirty": bool(status) if status is not None else None}


def sysinfo():
    spec = importlib.util.spec_from_file_location("sysinfo", CODE / "setup" / "sysinfo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.collect()


def calibrate(wlgen, cpu, trials=9):
    out = subprocess.run([wlgen, "calibrate", "--trials", str(trials)], capture_output=True, text=True,
                         check=True, preexec_fn=lambda: os.sched_setaffinity(0, {cpu}))
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("--batch-id", help="default: <config-name>_<timestamp>")
    ap.add_argument("--resume", help="existing batch directory; runs with status ok are skipped")
    ap.add_argument("--only", help="run only run_ids containing this substring")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pack", action="store_true", help="tar.zst the batch directory at the end")
    args = ap.parse_args()

    cfg = load_config(args.config)
    name = Path(args.config).stem
    if args.resume:
        batch_dir = Path(args.resume).resolve()
        batch_id = batch_dir.name
    else:
        batch_id = args.batch_id or f"{name}_{time.strftime('%Y%m%d-%H%M%S')}"
        batch_dir = CODE / "results" / "raw" / batch_id
    runs = expand(cfg, batch_id)
    if args.only:
        runs = [r for r in runs if args.only in r["run_id"]]
    for r in runs:  # resolve early so a typo in a policy name fails before hours of runs
        baselines.resolve(r["policy"])

    per_run = 1.5 + cfg["warmup_s"] + cfg["duration_s"] + cfg["settle_s"] + 2
    print(f"{len(runs)} runs, ~{len(runs) * per_run / 3600:.2f} h  ->  {batch_dir}")
    if args.dry_run:
        for r in runs:
            print(f"  [{r['block']}:{r['order']:3d}] {r['run_id']}")
        return 0
    if os.geteuid() != 0:
        sys.exit("run_matrix.py: needs root (sudo)")

    wlgen, schedmon = str(CODE / "build" / "wlgen"), str(CODE / "build" / "schedmon")
    for exe in (wlgen, schedmon):
        if not os.access(exe, os.X_OK):
            sys.exit(f"missing {exe}: run make first")

    batch_dir.mkdir(parents=True, exist_ok=True)
    hk = cfg["housekeeping_cpu"]
    os.sched_setaffinity(0, {hk})
    (batch_dir / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (batch_dir / "policies.json").write_text(json.dumps(
        {p: baselines.resolve(p) for p in sorted({r["policy"] for r in runs})}, indent=2) + "\n")
    (batch_dir / "sysinfo.json").write_text(json.dumps(sysinfo(), indent=2, sort_keys=True) + "\n")

    calib_file = batch_dir / "calibration.json"
    if calib_file.exists():
        calib = json.loads(calib_file.read_text())
    elif cfg.get("iters_per_us"):
        calib = {"iters_per_us": cfg["iters_per_us"], "source": "config"}
    else:
        calib = calibrate(wlgen, cfg["first_cpu"])
        calib["source"] = "measured"
    calib_file.write_text(json.dumps(calib, indent=2) + "\n")

    cg = ExperimentCgroups()
    max_cores = max(cfg["cores"])
    cg.setup(f"{cfg['first_cpu']}-{cfg['first_cpu'] + max_cores - 1}" if max_cores > 1 else str(cfg["first_cpu"]))
    if cfg["isolate_housekeeping"]:
        isolate_housekeeping(str(hk))
    ctx = {"cgroups": cg, "wlgen": wlgen, "schedmon": schedmon, "iters_per_us": calib["iters_per_us"],
           "first_cpu": cfg["first_cpu"], "housekeeping_cpu": hk, "git": git_info()}

    failed_path = batch_dir / "failed_runs.csv"
    index = []
    t_begin = time.time()
    try:
        if cfg["noise_floor"] and not (batch_dir / "noise_floor" / "meta.json").exists():
            nf = cfg["noise_floor"]
            nf_mix = cfg["mixes"][nf["mix"]] if isinstance(nf["mix"], str) else nf["mix"]
            spec = {"run_id": "noise_floor", "batch_id": batch_id, "mix_name": "noise", "mix": nf_mix,
                    "contention": "x0", "batch_per_core": 0, "policy": "default", "cores": 1,
                    "seed": 1, "warmup_s": 1, "duration_s": nf.get("duration_s", 10),
                    "unit_us": cfg["unit_us"], "events": True}
            m = run_one(spec, batch_dir / "noise_floor", ctx)
            print(f"noise floor: {m['status']}")

        for i, spec in enumerate(runs):
            out = batch_dir / spec["run_id"]
            meta_file = out / "meta.json"
            if meta_file.exists() and json.loads(meta_file.read_text()).get("status") == "ok":
                index.append({"run_id": spec["run_id"], "status": "ok", "skipped": True})
                continue
            if out.exists():
                shutil.rmtree(out)
            meta = run_one(spec, out, ctx)
            index.append({"run_id": spec["run_id"], "status": meta["status"]})
            if meta["status"] != "ok":
                new = not failed_path.exists()
                with open(failed_path, "a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["run_id", "status", "time"])
                    w.writerow([spec["run_id"], meta["status"], meta["created"]])
            done = i + 1
            eta = (time.time() - t_begin) / done * (len(runs) - done)
            print(f"[{done}/{len(runs)}] {spec['run_id']}: {meta['status']}  (ETA {eta / 60:.0f} min)",
                  flush=True)
            time.sleep(cfg["settle_s"])
    except KeyboardInterrupt:
        print("interrupted; resume with --resume", batch_dir)
    finally:
        cg.teardown()
        if cfg["isolate_housekeeping"]:
            release_housekeeping()
        (batch_dir / "index.json").write_text(json.dumps(index, indent=2) + "\n")
        os.sched_setaffinity(0, cpu_set(0, os.cpu_count()))

    if args.pack:
        tar = batch_dir.with_suffix(".tar.zst")
        subprocess.run(["tar", "--zstd", "-cf", str(tar), "-C", str(batch_dir.parent), batch_dir.name],
                       check=True)
        print("packed", tar)
    n_bad = sum(1 for r in index if r["status"] != "ok")
    print(f"done: {len(index)} runs, {n_bad} failed")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
