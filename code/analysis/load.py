"""Readers for one run directory (layout in ../INTERFACES.md)."""
import json
from functools import cached_property
from pathlib import Path

import pandas as pd

HIST_COLS = [f"h{i:02d}" for i in range(27)]
CLASS_NAMES = {0: "other", 1: "interactive", 2: "batch"}


def _json(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def _csv(path, **kw):
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    return pd.read_csv(p, **kw)


class Run:
    def __init__(self, path):
        self.path = Path(path)

    def __repr__(self):
        return f"Run({self.path.name})"

    @cached_property
    def meta(self):
        return _json(self.path / "meta.json") or {}

    @property
    def ok(self):
        return self.meta.get("status") == "ok"

    @property
    def cfg(self):
        return self.meta.get("config", {})

    @property
    def t_meas(self):
        return self.meta["t_meas_start_ns"], self.meta["t_meas_end_ns"]

    @cached_property
    def requests(self):
        return _csv(self.path / "requests.csv")

    @cached_property
    def batch(self):
        return _csv(self.path / "batch.csv")

    @cached_property
    def batch_ts(self):
        return _csv(self.path / "batch_ts.csv")

    @cached_property
    def tasks(self):
        return _csv(self.path / "tasks.csv")

    @cached_property
    def events(self):
        return _csv(self.path / "events.csv")

    @cached_property
    def policy_log(self):
        return _csv(self.path / "policy_log.csv")

    @cached_property
    def stats(self):
        return _json(self.path / "schedmon_stats.json") or {}

    @cached_property
    def isum(self):
        return _json(self.path / "interactive_summary.json") or {}

    @cached_property
    def bsum(self):
        return _json(self.path / "batch_summary.json") or {}

    def measured_events(self):
        ev = self.events
        if ev is None:
            return None
        t0, t1 = self.t_meas
        return ev[(ev.ts_ns >= t0) & (ev.ts_ns < t1)]


def runs_in(batch_dir, include_failed=False):
    batch_dir = Path(batch_dir)
    out = []
    for meta in sorted(batch_dir.glob("*/meta.json")):
        r = Run(meta.parent)
        if r.meta.get("run_id") == "noise_floor":
            continue
        if r.ok or include_failed:
            out.append(r)
    return out
