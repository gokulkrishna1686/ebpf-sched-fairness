"""Execute one experiment run: cgroups + policy knobs, schedmon, two wlgen processes.

Library module used by run_matrix.py. One run produces one directory laid out as described
in INTERFACES.md, with meta.json written last (status "ok" or "failed:<reason>").
"""
import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

import baselines
from cgroups import ExperimentCgroups

BPF_STATS = Path("/proc/sys/kernel/bpf_stats_enabled")
INTERACTIVE_KEYS = ("tasks", "arrival", "rate", "on_ms", "off_ms", "period_us", "jitter_us",
                    "work_us", "work_dist", "work_sigma")


class RunFailed(Exception):
    pass


RESULTS = Path(__file__).resolve().parents[1] / "results"


def give_to_invoker(path):
    """Under sudo, hand `path` (recursively) and its parents up to results/ back to the invoking
    user, so the analysis can run unprivileged afterwards."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() != 0 or uid is None or gid is None:
        return
    uid, gid, path = int(uid), int(gid), Path(path)
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    parent = path.parent
    while RESULTS == parent or RESULTS in parent.parents:
        targets.append(parent)
        if parent == RESULTS:
            break
        parent = parent.parent
    for p in targets:
        try:
            os.chown(p, uid, gid, follow_symlinks=False)
        except OSError:
            pass


def cpu_range(first, n):
    return str(first) if n == 1 else f"{first}-{first + n - 1}"


def cpu_set(first, n):
    return set(range(first, first + n))


def _affinity(cpus):
    def fn():
        try:
            os.sched_setaffinity(0, cpus)
        except OSError:
            # The parent's cpuset excludes `cpus` (housekeeping isolation confines our own scope to the
            # housekeeping CPU, e.g. WSL's init.scope): allow any CPU instead. The experiment cgroup's
            # cpuset confines the process as soon as it joins.
            os.sched_setaffinity(0, range(os.cpu_count()))
    return fn


def _wait_ready(proc, timeout):
    """Block until schedmon prints READY (BPF programs loaded and attached)."""
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RunFailed(f"schedmon exited early (rc={proc.returncode})")
        r, _, _ = select.select([proc.stdout], [], [], 0.2)
        if r:
            chunk = os.read(proc.stdout.fileno(), 4096)
            buf += chunk
            if b"READY" in buf:
                return
    raise RunFailed("schedmon not ready in time")


def _stop(proc, timeout=30):
    if proc is None or proc.poll() is not None:
        return None if proc is None else proc.returncode
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -9


def interactive_args(mix):
    args = []
    for key in INTERACTIVE_KEYS:
        if key in mix:
            args += ["--" + key.replace("_", "-"), str(mix[key])]
    return args


def run_one(spec, out_dir, ctx):
    """spec: run description (see run_matrix.expand); ctx: shared paths and objects."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cg: ExperimentCgroups = ctx["cgroups"]
    pol = baselines.resolve(spec["policy"])
    cores = spec["cores"]
    cpus = cpu_range(ctx["first_cpu"], cores)
    n_batch = int(round(spec["batch_per_core"] * cores))
    inter = dict(spec["mix"].get("interactive", {}))
    n_inter = int(inter.get("tasks", 0))

    meta = {
        "run_id": spec["run_id"], "batch_id": spec["batch_id"],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git": ctx["git"], "sysinfo_file": "../sysinfo.json",
        "config": {
            "mix": spec["mix_name"], "mix_params": spec["mix"], "contention": spec["contention"],
            "batch_per_core": spec["batch_per_core"], "batch_threads": n_batch,
            "interactive_tasks": n_inter, "cores": cores, "policy": spec["policy"],
            "policy_resolved": pol, "seed": spec["seed"], "warmup_s": spec["warmup_s"],
            "duration_s": spec["duration_s"], "unit_us": spec["unit_us"],
            "iters_per_us": ctx["iters_per_us"], "events": spec["events"],
            "bpf_stats": spec.get("bpf_stats", False), "block": spec.get("block"),
            "order": spec.get("order"),
        },
        "cpuset": cpus, "housekeeping_cpu": ctx["housekeeping_cpu"],
        "cgroups": {c: str(cg.path(c)) for c in ("interactive", "batch")},
        "commands": {}, "status": "running",
    }
    procs = {}
    files = []
    sm = None
    status = "ok"
    try:
        cg.reset()
        cg.set_cpus(cpus)
        meta["cpuset_effective"] = cg.effective_cpus()
        weights = pol.get("cg_weight", {})
        for child in ("interactive", "batch"):
            cg.set_weight(child, weights.get(child, 100))
        meta["cg_weight_before"] = {c: cg.weight(c) for c in ("interactive", "batch")}
        if spec.get("bpf_stats"):
            BPF_STATS.write_text("1")

        if pol["collector"]:
            argv = [ctx["schedmon"], "--cg-interactive", str(cg.path("interactive")),
                    "--cg-batch", str(cg.path("batch")), "--out", str(out_dir),
                    "--cpu", str(ctx["housekeeping_cpu"])]
            if spec["events"] and pol.get("events", True):
                argv.append("--events")
            argv += baselines.schedmon_args(pol)
            meta["commands"]["schedmon"] = argv
            err = open(out_dir / "schedmon.stderr", "w")
            files.append(err)
            sm = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err,
                                  preexec_fn=_affinity({ctx["housekeeping_cpu"]}))
            _wait_ready(sm, timeout=30)

        start_at = time.monotonic_ns() + int(1.5e9)
        common = ["--seed", str(spec["seed"]), "--start-at", str(start_at),
                  "--warmup-s", str(spec["warmup_s"]), "--duration-s", str(spec["duration_s"]),
                  "--out", str(out_dir), "--iters-per-us", f"{ctx['iters_per_us']:.6f}"]
        exp_cpus = cpu_set(ctx["first_cpu"], cores)
        if n_batch > 0:
            argv = [ctx["wlgen"], "batch", "--threads", str(n_batch), "--unit-us", str(spec["unit_us"]),
                    "--cgroup", str(cg.path("batch"))] + common
            if "batch_nice" in pol:
                argv += ["--nice", str(pol["batch_nice"])]
            procs["batch"] = argv
        if n_inter > 0:
            argv = [ctx["wlgen"], "interactive"] + interactive_args(inter) + \
                   ["--cgroup", str(cg.path("interactive"))] + common
            if "interactive_nice" in pol:
                argv += ["--nice", str(pol["interactive_nice"])]
            if "interactive_slice_us" in pol:
                argv += ["--slice-us", str(pol["interactive_slice_us"])]
            procs["interactive"] = argv
        running = {}
        for role, argv in procs.items():
            meta["commands"][role] = argv
            out = open(out_dir / f"wlgen_{role}.stdout", "w")
            err = open(out_dir / f"wlgen_{role}.stderr", "w")
            files += [out, err]
            running[role] = subprocess.Popen(argv, stdout=out, stderr=err, preexec_fn=_affinity(exp_cpus))
        meta["t0_ns"] = start_at
        meta["t_meas_start_ns"] = start_at + int(spec["warmup_s"] * 1e9)
        meta["t_meas_end_ns"] = meta["t_meas_start_ns"] + int(spec["duration_s"] * 1e9)

        budget = 1.5 + spec["warmup_s"] + spec["duration_s"] + 20
        deadline = time.time() + budget
        for role, p in running.items():
            try:
                rc = p.wait(timeout=max(1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                raise RunFailed(f"wlgen {role} timed out")
            if rc != 0:
                raise RunFailed(f"wlgen {role} rc={rc}")
    except RunFailed as e:
        status = f"failed:{e}"
    except Exception as e:  # keep the matrix going; record the failure
        status = f"failed:{type(e).__name__}:{e}"
    finally:
        rc = _stop(sm)
        if rc not in (None, 0):
            status = status if status != "ok" else f"failed:schedmon rc={rc}"
        meta["schedmon_rc"] = rc
        if spec.get("bpf_stats"):
            try:
                BPF_STATS.write_text("0")
            except OSError:
                pass
        # Restore check: the policy must have put every knob it touched back.
        leftover = {c: cg.procs(c) for c in ("interactive", "batch")}
        weights_after = {c: cg.weight(c) for c in ("interactive", "batch")}
        restore_ok = (not any(leftover.values())) and weights_after == meta.get("cg_weight_before")
        meta["restore_check"] = {"ok": restore_ok, "leftover_procs": leftover,
                                 "cg_weight_after": weights_after}
        if not restore_ok and status == "ok":
            status = "failed:restore_check"
        cg.reset()
        for f in files:
            f.close()

    for name in (["requests.csv"] if n_inter else []) + (["batch.csv"] if n_batch else []) + \
            (["tasks.csv", "schedmon_stats.json"] if pol["collector"] else []):
        if status == "ok" and not (out_dir / name).exists():
            status = f"failed:missing {name}"
    meta["status"] = status
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
