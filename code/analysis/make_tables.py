"""LaTeX tables and number macros for the paper, generated from the summary CSVs.

  python analysis/make_tables.py --main MAIN_ID [--overhead ID] [--ablation ID] [--heldout ID]
                                 [--out ../report/tables]

Writes:
  tab_main.tex      per mix: policy x {p99, p99/B0, SLO miss, batch tput/B0, Jain, batch max wait}
  tab_heldout.tex   the same for the held-out mix
  tab_overhead.tex  BPF ns/event, BPF CPU share, drop rate, end-to-end ratios
  tab_repro.tex     platform table from sysinfo.json
  numbers.tex       \\newcommand macros for every number the text quotes
Do not type result numbers into the paper by hand: \\input{tables/numbers.tex} and use the macros.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import style  # noqa: E402

CODE = Path(__file__).resolve().parents[1]
DIGITS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six",
          "7": "Seven", "8": "Eight", "9": "Nine"}


def macro_name(*parts):
    s = "".join(p.title() for p in parts)
    s = "".join(DIGITS.get(ch, ch) for ch in s)
    return "res" + re.sub(r"[^A-Za-z]", "", s)


def fmt(v, digits=2):
    return "--" if v is None or not np.isfinite(v) else f"{v:.{digits}f}"


def ci(row, col, scale=1.0, digits=1):
    v, lo, hi = row.get(col), row.get(col + "_lo"), row.get(col + "_hi")
    if v is None or not np.isfinite(v):
        return "--"
    return f"{v * scale:.{digits}f} {{\\scriptsize[{lo * scale:.{digits}f}, {hi * scale:.{digits}f}]}}"


def esc(s):
    return s.replace("_", "\\_").replace("θ", "$\\theta$").replace("µ", "$\\mu$")


def main_table(cells, paired, conts, caption, label):
    lines = [
        "\\begin{table*}[t]", "\\centering", "\\footnotesize", f"\\caption{{{caption}}}", f"\\label{{{label}}}",
        "\\begin{tabular}{llrrrrrr}", "\\toprule",
        "Mix & Policy & p99 (ms) [95\\% CI] & p99/B0 & SLO miss (\\%) & Batch tput/B0 & Jain (norm.) & Batch max wait (ms) \\\\",
        "\\midrule",
    ]
    for cont in conts:
        lines.append(f"\\multicolumn{{8}}{{l}}{{\\textit{{Contention {cont} ({cont[1:]} batch threads per core)}}}} \\\\")
        for mix in sorted(cells.mix.unique()):
            sub = cells[(cells.mix == mix) & (cells.contention == cont)].set_index("policy")
            pr = paired[(paired.mix == mix) & (paired.contention == cont)].set_index("policy") if paired is not None else None
            for k, pol in enumerate(style.ordered(sub.index)):
                row = sub.loc[pol]
                ratio = pr.loc[pol] if pr is not None and pol in pr.index else None
                lines.append(" & ".join([
                    mix if k == 0 else "", esc(style.label(pol)), ci(row, "rt_p99_us", 1e-3),
                    "1.00" if pol == "default" else fmt(ratio["rt_p99_us_ratio"] if ratio is not None else None),
                    fmt(row.get("slo_viol", np.nan) * 100, 1),
                    "1.00" if pol == "default" else fmt(ratio["batch_tput_ratio"] if ratio is not None else None, 3),
                    fmt(row.get("jain_norm_share"), 3), fmt(row.get("wait_batch_max_us", np.nan) / 1e3, 1),
                ]) + " \\\\")
            lines.append("\\addlinespace[2pt]")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}"]
    return "\n".join(lines) + "\n"


def overhead_table(cells, runs):
    order = ["default-nocollector", "default-noevents", "default"]
    names = {"default-nocollector": "no collector", "default-noevents": "maps only", "default": "maps + events"}
    lines = ["\\begin{table}[t]", "\\centering", "\\footnotesize",
             "\\caption{Instrumentation overhead (median over seeds; ratios vs.\\ no collector).}",
             "\\label{tab:overhead}", "\\begin{tabular}{llrrrrr}", "\\toprule",
             "Cont. & Collector & ns/event & BPF CPU (\\%) & Drops (\\%) & Tput ratio & p99 ratio \\\\", "\\midrule"]
    for cont in sorted(cells.contention.unique()):
        sub = cells[cells.contention == cont].set_index("policy")
        base = sub.loc[order[0]] if order[0] in sub.index else None
        for k, pol in enumerate(p for p in order if p in sub.index):
            r = sub.loc[pol]
            lines.append(" & ".join([
                cont if k == 0 else "", names[pol], fmt(r.get("bpf_ns_per_event"), 0),
                fmt(r.get("bpf_cpu_frac", np.nan) * 100, 3), fmt(r.get("drop_rate", np.nan) * 100, 3),
                fmt(r["batch_tput"] / base["batch_tput"], 3) if base is not None else "--",
                fmt(r["rt_p99_us"] / base["rt_p99_us"], 2) if base is not None else "--",
            ]) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def repro_table(sysinfo, calib):
    cpu = sysinfo.get("cpu") or {}
    kc = sysinfo.get("kernel_config") or {}
    tc = sysinfo.get("toolchain") or {}
    sch = sysinfo.get("sched") or {}
    py = sysinfo.get("python") or {}
    rows = [
        ("Distribution", sysinfo.get("distro")),
        ("Kernel", sysinfo.get("kernel_release")),
        ("Virtualization", f"{sysinfo.get('virtualization')} (clocksource {sysinfo.get('clocksource')})"),
        ("CPU", f"{cpu.get('Model name')} ({cpu.get('CPU(s)')} vCPU)"),
        ("Memory", f"{(sysinfo.get('mem_total_kib') or 0) / 1048576:.1f} GiB"),
        ("Storage", (sysinfo.get("storage") or {}).get("root_fs")),
        ("Kernel config", f"HZ={kc.get('CONFIG_HZ')}, PREEMPT\\_DYNAMIC={kc.get('CONFIG_PREEMPT_DYNAMIC')}, "
                          f"SCHED\\_CLASS\\_EXT={kc.get('CONFIG_SCHED_CLASS_EXT')}"),
        ("Scheduler", f"EEVDF, base slice {sch.get('base_slice_ns')} ns, preempt={sch.get('preempt')}"),
        ("Compilers", f"{tc.get('gcc')}; {tc.get('clang')}"),
        ("BPF tooling", f"libbpf {tc.get('libbpf')}; {tc.get('bpftool')}; {tc.get('bpftrace')}"),
        ("Analysis", f"Python {py.get('python')}, numpy {py.get('numpy')}, pandas {py.get('pandas')}, scipy {py.get('scipy')}"),
        ("Spin calibration", f"{calib.get('iters_per_us', float('nan')):.1f} iterations/\\textmu s"),
    ]
    lines = ["\\begin{table}[t]", "\\centering", "\\footnotesize", "\\caption{Experimental platform (from sysinfo.json).}",
             "\\label{tab:repro}", "\\begin{tabular}{@{}lp{0.68\\columnwidth}@{}}", "\\toprule"]
    lines += [f"{k} & {esc(str(v)) if v is not None else '--'} \\\\" for k, v in rows]
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def number_macros(tag, cells, paired):
    out = [f"% generated by analysis/make_tables.py from batch {tag}; do not edit"]
    if cells is not None:
        for _, r in cells.iterrows():
            base = macro_name(tag, r.mix, r.contention, r.policy)
            for col, scale, d in (("rt_p99_us", 1e-3, 1), ("slo_viol", 100, 1), ("batch_tput", 1, 0),
                                  ("wait_batch_max_us", 1e-3, 1), ("jain_norm_share", 1, 3)):
                if col in r and np.isfinite(r[col]):
                    out.append(f"\\newcommand{{\\{base}{macro_name(col)[3:]}}}{{{r[col] * scale:.{d}f}}}")
    if paired is not None:
        for _, r in paired.iterrows():
            base = macro_name(tag, r.mix, r.contention, r.policy)
            for col in ("rt_p99_us_ratio", "batch_tput_ratio"):
                if col in r and np.isfinite(r[col]):
                    out.append(f"\\newcommand{{\\{base}{macro_name(col)[3:]}}}{{{r[col]:.2f}}}")
                    # percent change, e.g. "-42" for a ratio of 0.58
                    out.append(f"\\newcommand{{\\{base}{macro_name(col)[3:]}Pct}}{{{(r[col] - 1) * 100:+.0f}}}")
    return "\n".join(out) + "\n"


def read(summary, batch, kind):
    p = summary / f"{batch}_{kind}.csv"
    return pd.read_csv(p) if p.exists() and p.stat().st_size > 1 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", default=str(CODE / "results" / "summary"))
    ap.add_argument("--raw", default=str(CODE / "results" / "raw"))
    ap.add_argument("--out", default=str(CODE.parent / "report" / "tables"))
    ap.add_argument("--main")
    ap.add_argument("--heldout")
    ap.add_argument("--overhead")
    ap.add_argument("--ablation")
    ap.add_argument("--conts", default="x2,x4")
    args = ap.parse_args()
    S, R, O = Path(args.summary), Path(args.raw), Path(args.out)
    O.mkdir(parents=True, exist_ok=True)
    macros = []
    if args.main:
        cells, paired = read(S, args.main, "cells"), read(S, args.main, "paired")
        (O / "tab_main.tex").write_text(main_table(
            cells, paired, args.conts.split(","),
            "Main results: median over 5 paired seeds with 95\\% bootstrap CI; ratios are paired with B0.",
            "tab:main"))
        macros.append(number_macros("main", cells, paired))
        si, cal = R / args.main / "sysinfo.json", R / args.main / "calibration.json"
        if si.exists():
            (O / "tab_repro.tex").write_text(repro_table(json.loads(si.read_text()),
                                                         json.loads(cal.read_text()) if cal.exists() else {}))
    if args.heldout:
        cells, paired = read(S, args.heldout, "cells"), read(S, args.heldout, "paired")
        (O / "tab_heldout.tex").write_text(main_table(
            cells, paired, ["x1", "x2", "x4"], "Held-out mix M4 (never used for tuning).", "tab:heldout"))
        macros.append(number_macros("heldout", cells, paired))
    if args.overhead:
        (O / "tab_overhead.tex").write_text(overhead_table(read(S, args.overhead, "cells"),
                                                           read(S, args.overhead, "runs")))
    if args.ablation:
        macros.append(number_macros("ablation", read(S, args.ablation, "cells"), read(S, args.ablation, "paired")))
    (O / "numbers.tex").write_text("\n".join(macros))
    print("tables written to", O)


if __name__ == "__main__":
    main()
