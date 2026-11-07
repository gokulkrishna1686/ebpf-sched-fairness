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
import stats  # noqa: E402
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
    """Collector cost per contention level. End-to-end ratios are paired by seed with the
    no-collector runs (median and 95% bootstrap CI), like every other comparison in the paper."""
    names = {"default-noevents": "maps only", "default": "maps + events"}
    pr = stats.paired(runs, baseline="default-nocollector", metrics=["batch_tput", "rt_p99_us"]) if runs is not None else None
    drops = cells.drop_rate.max() if "drop_rate" in cells else np.nan
    lines = ["\\begin{table}[t]", "\\centering", "\\footnotesize",
             "\\caption{Instrumentation overhead. Ratios are paired by seed with runs without the collector "
             f"(median [95\\% CI]); the maximum ring-buffer drop rate was {drops * 100:.3f}\\%.}}",
             "\\label{tab:overhead}", "\\setlength{\\tabcolsep}{3.5pt}", "\\begin{tabular}{@{}llrrrr@{}}", "\\toprule",
             "& Collector & ns/ev. & CPU\\,\\% & Tput ratio & p99 ratio \\\\", "\\midrule"]
    for cont in sorted(cells.contention.unique()):
        sub = cells[cells.contention == cont].set_index("policy")
        for k, pol in enumerate(p for p in names if p in sub.index):
            r = sub.loc[pol]
            q = pr[(pr.contention == cont) & (pr.policy == pol)] if pr is not None else pd.DataFrame()
            q = q.iloc[0] if len(q) else {}
            lines.append(" & ".join([
                cont if k == 0 else "", names[pol], fmt(r.get("bpf_ns_per_event"), 0),
                fmt(r.get("bpf_cpu_frac", np.nan) * 100, 3),
                ci(q, "batch_tput_ratio", digits=2) if len(q) else "--",
                ci(q, "rt_p99_us_ratio", digits=2) if len(q) else "--",
            ]) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def _mark(v, lo, hi, lower_is_better=True):
    """Bold: the whole 95% CI is on the good side of 1; italic: the whole CI is on the bad side."""
    s = fmt(v)
    if not np.isfinite(lo) or not np.isfinite(hi):
        return s
    good = hi < 1 if lower_is_better else lo > 1
    bad = lo > 1 if lower_is_better else hi < 1
    return f"\\textbf{{{s}}}" if good else f"\\textit{{{s}}}" if bad else s


def pooled_pairs(runs, pol, metric, baseline="default"):
    """All (mix, contention, seed) pairs of one policy with the baseline, pooled into one ratio sample."""
    key = ["mix", "contention", "cores", "seed"]
    b = runs[runs.policy == baseline].set_index(key)[metric]
    a = runs[runs.policy == pol].set_index(key)[metric]
    k = a.index.intersection(b.index)
    r = a.loc[k].to_numpy(dtype=float) / b.loc[k].to_numpy(dtype=float)
    return r[np.isfinite(r) & (r > 0)]


def ratio_matrix(main_p, main_cells, main_runs, held_p, held_cells, caption, label):
    """One compact table for the paper: every policy x every (mix, contention) cell, as ratios to B0.
    Main mixes, a pooled main column, then the held-out mix."""
    conts = ["x1", "x2", "x4"]
    groups = [(m, main_p, main_cells) for m in sorted(main_p.mix.unique())]
    if held_p is not None:
        groups += [(m, held_p, held_cells) for m in sorted(held_p.mix.unique())]
    pols = [p for p in style.ordered(set(main_p.policy)) if p != "default"]
    ncol = 1 + 3 * len(groups) + (1 if main_runs is not None else 0)
    head1, head2, rules, c0 = ["Policy"], [""], [], 2
    for k, (m, _, _) in enumerate(groups):
        if main_runs is not None and k == len(sorted(main_p.mix.unique())):
            head1.append("\\multicolumn{1}{c}{All}")
            head2.append("pairs")
            c0 += 1
        held = held_p is not None and m in set(held_p.mix)
        head1.append(f"\\multicolumn{{3}}{{c}}{{{m}{' (held out)' if held else ''}}}")
        head2 += conts
        rules.append(f"\\cmidrule(lr){{{c0}-{c0 + 2}}}")
        c0 += 3
    if main_runs is not None and len(groups) == len(sorted(main_p.mix.unique())):
        head1.append("\\multicolumn{1}{c}{All}")
        head2.append("pairs")
    lines = ["\\begin{table*}[t]", "\\centering", "\\footnotesize", f"\\caption{{{caption}}}", f"\\label{{{label}}}",
             "\\setlength{\\tabcolsep}{4.2pt}", f"\\begin{{tabular}}{{@{{}}l{'r' * (ncol - 1)}@{{}}}}", "\\toprule",
             " & ".join(head1) + " \\\\", "".join(rules), " & ".join(head2) + " \\\\", "\\midrule"]

    def cells_row(name, getter, pooled_getter=None):
        out = [name]
        for k, (m, p, c) in enumerate(groups):
            if pooled_getter is not None and k == len(sorted(main_p.mix.unique())):
                out.append(pooled_getter())
            for cont in conts:
                out.append(getter(m, cont, p, c))
        if pooled_getter is not None and len(groups) == len(sorted(main_p.mix.unique())):
            out.append(pooled_getter())
        return " & ".join(out) + " \\\\"

    def b0(m, cont, p, c):
        r = c[(c.mix == m) & (c.contention == cont) & (c.policy == "default")]
        return fmt(r.rt_p99_us.iloc[0] / 1e3, 1) if len(r) else "--"

    lines.append(cells_row("B0 p99 (ms)", b0, (lambda: "") if main_runs is not None else None))
    for metric, title, lower in (("rt_p99_us", "Interactive p99 / B0", True), ("batch_tput", "Batch throughput / B0", False)):
        lines.append(f"\\addlinespace[2pt]\\multicolumn{{{ncol}}}{{@{{}}l}}{{\\textit{{{title}}}}} \\\\")
        for pol in pols:
            def get(m, cont, p, c, pol=pol, metric=metric, lower=lower):
                r = p[(p.mix == m) & (p.contention == cont) & (p.policy == pol)]
                if not len(r):
                    return "--"
                r = r.iloc[0]
                return _mark(r[metric + "_ratio"], r[metric + "_ratio_lo"], r[metric + "_ratio_hi"], lower)

            def pooled(pol=pol, metric=metric, lower=lower):
                v, lo, hi = stats.bootstrap_ci(pooled_pairs(main_runs, pol, metric))
                return _mark(v, lo, hi, lower)

            lines.append(cells_row(esc(style.label(pol)), get, pooled if main_runs is not None else None))
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}"]
    return "\n".join(lines) + "\n"


ABLATION_ROWS = [("waitboost-nice", "WB-nice"), ("waitboost-nice-noaging", "\\quad no aging"),
                 ("waitboost-nice-nofeedback", "\\quad no wait feedback"), ("waitboost-slice", "WB-slice"),
                 ("waitboost-slice-noaging", "\\quad no aging"), ("waitboost-slice-nofeedback", "\\quad no wait feedback"),
                 ("waitboost-cg", "WB-cg (per class)")]


def ablation_table(runs, caption, label):
    """Ablations pooled over the three mixes (15 seed pairs per row): median ratio to B0 [95% CI]."""
    lines = ["\\begin{table}[t]", "\\centering", "\\footnotesize", f"\\caption{{{caption}}}", f"\\label{{{label}}}",
             "\\setlength{\\tabcolsep}{4pt}", "\\begin{tabular}{@{}lrrrr@{}}", "\\toprule",
             "Variant & p99/B0 [95\\% CI] & Tput & Max wait & Boosts \\\\", "\\midrule"]
    for pol, name in ABLATION_ROWS:
        if not (runs.policy == pol).any():
            continue
        p99 = stats.bootstrap_ci(pooled_pairs(runs, pol, "rt_p99_us"))
        tput = stats.bootstrap_ci(pooled_pairs(runs, pol, "batch_tput"))
        mw = stats.bootstrap_ci(pooled_pairs(runs, pol, "wait_batch_max_us"))
        boosts = runs[runs.policy == pol].act_boost.median() if "act_boost" in runs else np.nan
        lines.append(" & ".join([
            name, f"{_mark(*p99)} {{\\scriptsize[{p99[1]:.2f}, {p99[2]:.2f}]}}", _mark(*tput, lower_is_better=False),
            _mark(*mw), fmt(boosts, 0)]) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def _storage(df_line):
    """'/dev/sdd ext4 1007G 2.9G 953G 1% /' -> 'ext4, 1007G'."""
    f = str(df_line or "").split()
    return f"{f[1]}, {f[2]}" if len(f) >= 3 else (df_line or None)


def _preempt(s):
    """debugfs lists all modes with the active one in parentheses: '(none) voluntary full lazy'."""
    m = re.search(r"\((\w+)\)", str(s or ""))
    return m.group(1) if m else s


def _ms(ns):
    try:
        return f"{int(ns) / 1e6:g} ms"
    except (TypeError, ValueError):
        return "--"


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
        ("Storage", _storage((sysinfo.get("storage") or {}).get("root_fs"))),
        # plain underscores: esc() below escapes every row once
        ("Kernel config", f"HZ={kc.get('CONFIG_HZ')}, PREEMPT_DYNAMIC={kc.get('CONFIG_PREEMPT_DYNAMIC')}, "
                          f"SCHED_CLASS_EXT={kc.get('CONFIG_SCHED_CLASS_EXT')}"),
        ("Scheduler", f"EEVDF, base slice {_ms(sch.get('base_slice_ns'))}, preemption mode {_preempt(sch.get('preempt'))}"),
        ("Compilers", f"{tc.get('gcc')}; {tc.get('clang')}"),
        ("BPF tooling", f"libbpf {tc.get('libbpf')}; {tc.get('bpftool')}; {tc.get('bpftrace')}"),
        ("Analysis", f"Python {py.get('python')}, numpy {py.get('numpy')}, pandas {py.get('pandas')}, scipy {py.get('scipy')}"),
        ("Spin calibration", f"{calib.get('iters_per_us', float('nan')):.1f} iterations/\\textmu s"),
    ]
    lines = ["\\begin{table}[t]", "\\centering", "\\footnotesize", "\\caption{Experimental platform (from sysinfo.json).}",
             "\\label{tab:repro}", "\\begin{tabular}{@{}l>{\\raggedright\\arraybackslash}p{0.72\\columnwidth}@{}}",
             "\\toprule"]
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
    try:
        return pd.read_csv(p)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return None


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
    ids = [b for b in (args.main, args.heldout, args.overhead, args.ablation) if b]
    note = ""
    if any(b.startswith(("pilot_", "quick_", "fake")) for b in ids):
        print("WARNING: test/pilot/synthetic batch: these tables are NOT results; never copy them into report/")
        note = "\\textbf{[TEST DATA -- NOT RESULTS]} "
    macros = []
    if args.main:
        cells, paired = read(S, args.main, "cells"), read(S, args.main, "paired")
        (O / "tab_main.tex").write_text(main_table(
            cells, paired, args.conts.split(","),
            note + "Main results: median over 5 paired seeds with 95\\% bootstrap CI; ratios are paired with B0.",
            "tab:main"))
        macros.append(number_macros("main", cells, paired))
        si, cal = R / args.main / "sysinfo.json", R / args.main / "calibration.json"
        if si.exists():
            (O / "tab_repro.tex").write_text(repro_table(json.loads(si.read_text()),
                                                         json.loads(cal.read_text()) if cal.exists() else {}))
    if args.heldout:
        cells, paired = read(S, args.heldout, "cells"), read(S, args.heldout, "paired")
        (O / "tab_heldout.tex").write_text(main_table(
            cells, paired, ["x1", "x2", "x4"], note + "Held-out mix M4 (never used for tuning).", "tab:heldout"))
        macros.append(number_macros("heldout", cells, paired))
    if args.main:
        mp, mc, mr = read(S, args.main, "paired"), read(S, args.main, "cells"), read(S, args.main, "runs")
        if mr is not None:
            mr = mr[mr.contention != "x0"]
        hp = read(S, args.heldout, "paired") if args.heldout else None
        hc = read(S, args.heldout, "cells") if args.heldout else None
        (O / "tab_matrix.tex").write_text(ratio_matrix(
            mp, mc, mr, hp, hc,
            # IEEEtran table captions are small caps; \normalfont avoids the missing italic small-caps shape
            note + "Every policy in every cell, relative to B0 (median of 5 seed pairs; All: the 45 main "
            "pairs pooled). \\textbf{Bold}: the whole 95\\% bootstrap CI is better than B0; "
            "{\\normalfont\\itshape italic}: the whole CI is worse. x$k$: $k$ batch threads per core.", "tab:matrix"))
    if args.overhead:
        (O / "tab_overhead.tex").write_text(overhead_table(read(S, args.overhead, "cells"),
                                                           read(S, args.overhead, "runs")))
    if args.ablation:
        macros.append(number_macros("ablation", read(S, args.ablation, "cells"), read(S, args.ablation, "paired")))
        ar = read(S, args.ablation, "runs")
        if ar is not None:
            (O / "tab_ablation.tex").write_text(ablation_table(
                ar, note + "Ablations at 2 batch threads per core, pooled over M1--M3 (15 seed pairs per row; "
                "marks as in Table~\\ref{tab:matrix}). Boosts: median boost actions per run.", "tab:ablation"))
    (O / "numbers.tex").write_text("\n".join(macros))
    print("tables written to", O)


if __name__ == "__main__":
    main()
