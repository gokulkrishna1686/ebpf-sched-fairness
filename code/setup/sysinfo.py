#!/usr/bin/env python3
"""Record the platform an experiment ran on (the brief's reproducibility checklist).

Usage: python3 sysinfo.py [--out FILE]

Every probe is best-effort: a missing tool or an unreadable file yields null, never an
exception. A few files under /sys/kernel/debug need root; run under sudo for the full record.
"""
import argparse
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import sys

KCONFIG_KEYS = [
    "CONFIG_BPF_SYSCALL", "CONFIG_BPF_JIT", "CONFIG_DEBUG_INFO_BTF", "CONFIG_BPF_EVENTS",
    "CONFIG_SCHEDSTATS", "CONFIG_SCHED_CLASS_EXT", "CONFIG_FAIR_GROUP_SCHED",
    "CONFIG_CGROUP_SCHED", "CONFIG_SCHED_AUTOGROUP", "CONFIG_HZ", "CONFIG_NO_HZ_IDLE",
    "CONFIG_NO_HZ_FULL", "CONFIG_PREEMPT", "CONFIG_PREEMPT_VOLUNTARY", "CONFIG_PREEMPT_NONE",
    "CONFIG_PREEMPT_DYNAMIC", "CONFIG_PREEMPT_LAZY", "CONFIG_SCHED_CORE", "CONFIG_SCHED_SMT",
]

SCHED_DEBUG_FILES = ["base_slice_ns", "migration_cost_ns", "nr_migrate", "preempt",
                     "features", "tunable_scaling"]


def run(cmd):
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr).strip()
    return text or None


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def first_line(text):
    return text.splitlines()[0] if text else None


def os_release():
    info = {}
    for line in (read("/etc/os-release") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip('"')
    return info.get("PRETTY_NAME")


def kernel_config():
    text = read(f"/boot/config-{platform.release()}")
    if text is None:
        try:
            with gzip.open("/proc/config.gz", "rt") as f:
                text = f.read()
        except OSError:
            return None
    cfg = {}
    for key in KCONFIG_KEYS:
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        cfg[key] = m.group(1) if m else "n"
    return cfg


def lscpu():
    text = run(["lscpu"])
    if not text:
        return None
    wanted = {"Model name", "CPU(s)", "Thread(s) per core", "Core(s) per socket", "Socket(s)",
              "NUMA node(s)", "Hypervisor vendor", "Virtualization type", "CPU max MHz",
              "L2 cache", "L3 cache"}
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() in wanted:
                out[k.strip()] = v.strip()
    return out


def meminfo_kib():
    m = re.search(r"^MemTotal:\s+(\d+)", read("/proc/meminfo") or "", re.M)
    return int(m.group(1)) if m else None


def storage():
    df = run(["df", "-hT", "/"])
    lsblk = run(["lsblk", "-d", "-o", "NAME,SIZE,ROTA,MODEL"])
    return {"root_fs": df.splitlines()[-1] if df else None, "disks": lsblk}


def sched_tunables():
    out = {}
    for name in SCHED_DEBUG_FILES:
        out[name] = read(f"/sys/kernel/debug/sched/{name}")
    for name in ["sched_autogroup_enabled", "sched_rt_runtime_us", "sched_rt_period_us",
                 "sched_schedstats", "bpf_stats_enabled"]:
        out[name] = read(f"/proc/sys/kernel/{name}")
    return out


def py_versions():
    vers = {"python": platform.python_version()}
    for mod in ["numpy", "pandas", "scipy", "matplotlib"]:
        try:
            vers[mod] = __import__(mod).__version__
        except Exception:
            vers[mod] = None
    return vers


def libbpf_version():
    v = run(["pkg-config", "--modversion", "libbpf"])
    if v:
        return v
    return run(["dpkg-query", "-W", "-f=${Version}", "libbpf-dev"])


def cpufreq():
    gov = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    return {"governor": gov, "driver": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")}


def collect():
    return {
        "distro": os_release(),
        "kernel_release": platform.release(),
        "kernel_version": read("/proc/version"),
        "kernel_cmdline": read("/proc/cmdline"),
        "kernel_config": kernel_config(),
        "virtualization": run(["systemd-detect-virt"]),
        "clocksource": read("/sys/devices/system/clocksource/clocksource0/current_clocksource"),
        "cpu": lscpu(),
        "cpufreq": cpufreq(),
        "online_cpus": read("/sys/devices/system/cpu/online"),
        "mem_total_kib": meminfo_kib(),
        "storage": storage(),
        "cgroup": {
            "controllers": read("/sys/fs/cgroup/cgroup.controllers"),
            "subtree_control": read("/sys/fs/cgroup/cgroup.subtree_control"),
        },
        "sched": sched_tunables(),
        "toolchain": {
            "gcc": first_line(run(["gcc", "--version"])),
            "clang": first_line(run(["clang", "--version"])),
            "bpftool": first_line(run(["bpftool", "version"])),
            "bpftrace": run(["bpftrace", "--version"]),
            "libbpf": libbpf_version(),
            "trace-cmd": first_line(run(["trace-cmd", "--version"])),
            "stress-ng": run(["stress-ng", "--version"]),
            "git": run(["git", "--version"]),
        },
        "python": py_versions(),
        "is_root": os.geteuid() == 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()
    info = collect()
    text = json.dumps(info, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
