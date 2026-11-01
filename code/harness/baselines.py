"""Policy registry: how each baseline / policy configures wlgen, the cgroups and schedmon.

A policy is a dict with any of these keys (all optional):
  batch_nice            nice value for every batch thread              (B1)
  interactive_nice      nice value for every interactive thread
  cg_weight             {"interactive": w, "batch": w} cpu.weight      (B2)
  interactive_slice_us  static EEVDF slice for interactive threads     (B3)
  collector             run schedmon at all (default True)
  events                stream per-wait events when the config enables them (default True)
  schedmon              dict of schedmon options; "policy": "waitboost" enables WaitBoost

Configs may define new policies as {"name": ..., "base": <existing>, <overrides>}; nested
"schedmon" dicts are merged, so a threshold sweep only overrides "theta_us".
"""
import copy
import json
from pathlib import Path

FROZEN_PARAMS = Path(__file__).resolve().parent / "configs" / "params_frozen.json"

POLICIES = {
    # B0: stock EEVDF, everything nice 0, equal cgroup weights.
    "default": {},
    # Overhead references: identical to default but without the collector / without event streaming.
    "default-nocollector": {"collector": False},
    "default-noevents": {"events": False},
    # B1: static priority: batch work at nice 10.
    "nice": {"batch_nice": 10},
    # B2: static cgroup weights (interactive cgroup 4x the batch cgroup).
    "cgweight": {"cg_weight": {"interactive": 400, "batch": 100}},
    # B3: static short EEVDF slice for interactive threads (Linux >= 6.12).
    "slice": {"interactive_slice_us": 300},
    # WaitBoost variants (parameters come from params_frozen.json, then per-policy overrides).
    "waitboost-nice": {"schedmon": {"policy": "waitboost", "actuator": "nice"}},
    "waitboost-slice": {"schedmon": {"policy": "waitboost", "actuator": "slice"}},
    "waitboost-cg": {"schedmon": {"policy": "waitboost", "actuator": "cgweight"}},
    # Ablations.
    "waitboost-slice-noaging": {"schedmon": {"policy": "waitboost", "actuator": "slice", "no_aging": True}},
    "waitboost-nice-noaging": {"schedmon": {"policy": "waitboost", "actuator": "nice", "no_aging": True}},
    "waitboost-slice-nofeedback": {"schedmon": {"policy": "waitboost", "actuator": "slice", "no_feedback": True}},
    "waitboost-nice-nofeedback": {"schedmon": {"policy": "waitboost", "actuator": "nice", "no_feedback": True}},
}

# schedmon option name -> command-line flag (booleans become bare flags).
SCHEDMON_FLAGS = {
    "policy": "--policy", "actuator": "--actuator", "interval_ms": "--interval-ms",
    "theta_us": "--theta-us", "alpha": "--alpha", "min_sleep": "--min-sleep",
    "cpu_cap": "--cpu-cap", "max_level": "--max-level", "decay_ms": "--decay-ms",
    "guard_ms": "--guard-ms", "nice_step": "--nice-step", "no_aging": "--no-aging",
    "no_feedback": "--no-feedback",
}


def frozen_params():
    """WaitBoost parameters fixed after tuning (see configs/params_frozen.json)."""
    if FROZEN_PARAMS.exists():
        data = json.loads(FROZEN_PARAMS.read_text())
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def register(custom):
    """Add config-defined policies: [{"name": n, "base": b, ...overrides}]."""
    for spec in custom or []:
        spec = dict(spec)
        name = spec.pop("name")
        base = spec.pop("base", "default")
        if base not in POLICIES:
            raise KeyError(f"policy {name}: unknown base {base}")
        POLICIES[name] = _merge(POLICIES[base], spec)


def resolve(name, use_frozen=True):
    """Fully resolved policy dict; WaitBoost policies get the frozen parameters underneath."""
    if name not in POLICIES:
        raise KeyError(f"unknown policy {name!r}; known: {', '.join(sorted(POLICIES))}")
    pol = copy.deepcopy(POLICIES[name])
    sm = pol.get("schedmon")
    if sm and sm.get("policy") == "waitboost" and use_frozen:
        frozen = frozen_params()
        per_actuator = frozen.pop("by_actuator", {})  # e.g. {"nice": {"theta_us": 1000}}
        base = _merge(frozen, per_actuator.get(sm.get("actuator", "nice"), {}))
        pol["schedmon"] = _merge(base, sm)  # explicit policy options (e.g. a theta sweep) win
    pol.setdefault("collector", True)
    return pol


def schedmon_args(pol):
    args = []
    for key, val in (pol.get("schedmon") or {}).items():
        flag = SCHEDMON_FLAGS.get(key)
        if flag is None:
            raise KeyError(f"unknown schedmon option {key!r}")
        if isinstance(val, bool):
            if val:
                args.append(flag)
        else:
            args += [flag, str(val)]
    return args
