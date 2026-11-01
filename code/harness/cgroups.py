"""cgroup v2 setup for one experiment: /sys/fs/cgroup/fairexp/{interactive,batch}.

The parent cgroup carries the cpuset (experiment CPUs). The two children carry cpu.weight;
their membership also tells the eBPF collector which class a thread belongs to.
"""
import os
import subprocess
import time
from pathlib import Path

CG_ROOT = Path("/sys/fs/cgroup")
CHILDREN = ("interactive", "batch")


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


class ExperimentCgroups:
    def __init__(self, name="fairexp"):
        self.parent = CG_ROOT / name

    def path(self, child):
        return self.parent / child

    def setup(self, cpus):
        # Enable the controllers down the hierarchy (no-ops if already enabled).
        for ctl in ("+cpu", "+cpuset"):
            try:
                _write(CG_ROOT / "cgroup.subtree_control", ctl)
            except OSError:
                pass
        self.parent.mkdir(exist_ok=True)
        _write(self.parent / "cpuset.cpus", cpus)
        _write(self.parent / "cgroup.subtree_control", "+cpu +cpuset")
        for child in CHILDREN:
            self.path(child).mkdir(exist_ok=True)
            _write(self.path(child) / "cpu.weight", 100)

    def set_cpus(self, cpus):
        _write(self.parent / "cpuset.cpus", cpus)

    def set_weight(self, child, weight):
        _write(self.path(child) / "cpu.weight", int(weight))

    def weight(self, child):
        v = _read(self.path(child) / "cpu.weight")
        return int(v) if v else None

    def effective_cpus(self):
        return _read(self.parent / "cpuset.cpus.effective")

    def procs(self, child):
        v = _read(self.path(child) / "cgroup.procs")
        return [int(x) for x in v.split()] if v else []

    def kill_all(self, timeout=5.0):
        for child in CHILDREN:
            if self.path(child).exists() and self.procs(child):
                try:
                    _write(self.path(child) / "cgroup.kill", 1)
                except OSError:
                    for pid in self.procs(child):
                        try:
                            os.kill(pid, 9)
                        except ProcessLookupError:
                            pass
        deadline = time.time() + timeout
        while time.time() < deadline and any(self.procs(c) for c in CHILDREN if self.path(c).exists()):
            time.sleep(0.05)

    def reset(self):
        """Back to the B0 state between runs."""
        self.kill_all()
        for child in CHILDREN:
            self.set_weight(child, 100)

    def teardown(self):
        if not self.parent.exists():
            return
        self.kill_all()
        for child in CHILDREN:
            try:
                self.path(child).rmdir()
            except OSError:
                pass
        try:
            self.parent.rmdir()
        except OSError:
            pass


HOUSEKEEPING_UNITS = ("system.slice", "user.slice", "init.scope")


def isolate_housekeeping(cpus):
    """Confine all system/user tasks to the housekeeping CPUs (systemd, runtime only)."""
    for unit in HOUSEKEEPING_UNITS:
        subprocess.run(["systemctl", "set-property", "--runtime", unit, f"AllowedCPUs={cpus}"],
                       check=False, capture_output=True)


def release_housekeeping():
    for unit in HOUSEKEEPING_UNITS:
        subprocess.run(["systemctl", "set-property", "--runtime", unit, "AllowedCPUs="],
                       check=False, capture_output=True)
