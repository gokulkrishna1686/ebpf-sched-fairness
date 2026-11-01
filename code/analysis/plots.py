"""Paper figures (PDF + PNG preview) from summary CSVs and raw run directories.

  python analysis/plots.py --main main_20261105-220000 [--ablation ID] [--heldout ID]
                           [--overhead ID] [--scaling ID] [--out ../report/figures]

Each figure is drawn only if its input batch is given. Run analysis/metrics.py and
analysis/stats.py first, because this module reads <batch>_{runs,cells,paired,threads}.csv.
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import style  # noqa: E402
from load import Run, runs_in  # noqa: E402

CODE = Path(__file__).resolve().parents[1]
SHORT = {"default": "B0", "nice": "B1", "cgweight": "B2", "slice": "B3", "waitboost-nice": "WB-n",
         "waitboost-slice": "WB-s", "waitboost-cg": "WB-cg"}
MAIN_POLICIES = ["default", "nice", "cgweight", "slice", "waitboost-nice", "waitboost-slice"]
CONT_LABEL = {"x1": "1 batch thr/core", "x2": "2 batch thr/core", "x4": "4 batch thr/core"}


def plain_log(ax, axis="x"):
    """Log scale with ticks at 1-2-5 and plain numbers (no 5x10^1 labels)."""
    loc = matplotlib.ticker.LogLocator(base=10, subs=(1.0, 2.0, 5.0))
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}")
    a = ax.xaxis if axis == "x" else ax.yaxis
    (ax.set_xscale if axis == "x" else ax.set_yscale)("log")
    a.set_major_locator(loc)
    a.set_major_formatter(fmt)
    a.set_minor_formatter(matplotlib.ticker.NullFormatter())


def save(fig, out, name):
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / f"{name}.pdf")
    fig.savefig(out / f"{name}.png", dpi=200)
    plt.close(fig)
    print("  wrote", out / f"{name}.pdf")


def read(summary, batch, kind):
    p = summary / f"{batch}_{kind}.csv"
    return pd.read_csv(p) if p.exists() and p.stat().st_size > 1 else None


# ------------------------------------------------------------------ figures


def fig_cdf(raw_batch, mix, out, conts=("x1", "x2", "x4")):
    """Response-time CDF per contention level, requests pooled over the evaluation seeds."""
    runs = [r for r in runs_in(raw_batch) if r.cfg["mix"] == mix]
    conts = [c for c in conts if any(r.cfg["contention"] == c for r in runs)]
    if not conts:
        return
    fig, axes = plt.subplots(1, len(conts), figsize=(style.PAGE_W * max(len(conts), 2) / 3, 1.9), sharey=True)
    for ax, cont in zip(np.atleast_1d(axes), conts):
        for pol in style.ordered({r.cfg["policy"] for r in runs if r.cfg["contention"] == cont}):
            rts = []
            for r in runs:
                if r.cfg["contention"] == cont and r.cfg["policy"] == pol and r.requests is not None:
                    m = r.requests[r.requests.measured == 1]
                    rts.append(((m.t_end_ns - m.t_arrival_ns) / 1e6).to_numpy())
            if not rts:
                continue
            x = np.sort(np.concatenate(rts))
            y = np.arange(1, len(x) + 1) / len(x)
            ax.plot(x, y, color=style.color(pol), label=style.label(pol), lw=1.1)
        slo = runs[0].cfg["mix_params"].get("slo_ms") if runs else None
        ax.set_ylim(0.5, 1.0)  # the tail is the story
        if slo:
            ax.axvline(slo, color=style.MUTED, lw=0.8, ls=(0, (3, 2)))
            ax.text(slo, 0.52, " SLO", color=style.INK_2, fontsize=6.5, va="bottom")
        plain_log(ax, "x")
        ax.set_title(f"{mix}, {CONT_LABEL.get(cont, cont)}")
        ax.set_xlabel("response time (ms)")
    np.atleast_1d(axes)[0].set_ylabel("CDF (upper half)")
    h, lab = np.atleast_1d(axes)[0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper center", ncol=len(lab), bbox_to_anchor=(0.5, 1.10))
    save(fig, out, f"cdf_{mix}")


def _pareto_panel(ax, sub, title):
    ax.axhline(1, color=style.AXIS, lw=0.8)
    ax.axvline(1, color=style.AXIS, lw=0.8)
    ax.plot([1], [1], marker=style.marker("default"), color=style.color("default"), ls="none", ms=5,
            mec="white", mew=0.6, label=style.label("default"))
    ax.annotate("B0", (1, 1), xytext=(3, 3), textcoords="offset points", fontsize=6.5, color=style.INK_2)
    sub = sub.set_index("policy")
    for pol in style.ordered(sub.index):
        row = sub.loc[pol]
        x, y = row.batch_tput_ratio, row.rt_p99_us_ratio
        xerr = [[x - row.batch_tput_ratio_lo], [row.batch_tput_ratio_hi - x]]
        yerr = [[y - row.rt_p99_us_ratio_lo], [row.rt_p99_us_ratio_hi - y]]
        ax.errorbar(x, y, xerr=xerr, yerr=yerr, fmt=style.marker(pol), color=style.color(pol), ms=5,
                    elinewidth=0.7, capsize=1.5, mec="white", mew=0.6, label=style.label(pol))
        ax.annotate(SHORT.get(pol, pol), (x, y), xytext=(4, -2), textcoords="offset points", fontsize=6.5,
                    color=style.INK_2)
    ax.set_title(title)
    ax.set_xlabel("batch throughput / B0")


def fig_pareto(paired, out, name, conts=("x2", "x4"), policies=MAIN_POLICIES):
    """Interactive p99 vs batch throughput, both relative to B0 (paired by seed); lower-right is better."""
    if paired is None:
        return
    p = paired[paired.policy.isin(policies)]
    conts = [c for c in conts if (p.contention == c).any()]
    if p.empty or not conts:
        return
    mixes = sorted(p.mix.unique())
    fig, axes = plt.subplots(len(conts), len(mixes), figsize=(style.PAGE_W, 1.85 * len(conts)),
                             squeeze=False, sharey="row")
    for i, cont in enumerate(conts):
        for j, mix in enumerate(mixes):
            _pareto_panel(axes[i, j], p[(p.mix == mix) & (p.contention == cont)], f"{mix}, {CONT_LABEL.get(cont, cont)}")
        axes[i, 0].set_ylabel("interactive p99 / B0")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.04))
    fig.tight_layout()
    save(fig, out, name)


def fig_waits(cells, mix, cont, out):
    """Run-queue wait p99 by class and the worst batch wait, per policy (two panels, one axis each)."""
    if cells is None:
        return
    c = cells[(cells.mix == mix) & (cells.contention == cont)]
    pols = style.ordered(c.policy.unique())
    c = c.set_index("policy").loc[pols]
    y = np.arange(len(pols))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(style.COL_W, 1.7), sharey=True)
    for ax, col, title in ((a1, "wait_inter_p99_us", "interactive wait p99"), (a2, "wait_batch_max_us", "batch max wait")):
        for k, pol in enumerate(pols):
            v, lo, hi = c.loc[pol, col] / 1e3, c.loc[pol, col + "_lo"] / 1e3, c.loc[pol, col + "_hi"] / 1e3
            ax.plot([lo, hi], [k, k], color=style.color(pol), lw=1.0)
            ax.plot(v, k, marker=style.marker(pol), color=style.color(pol), ms=5, mec="white", mew=0.6)
        plain_log(ax, "x")
        ax.set_title(title)
        ax.set_xlabel("ms")
    a1.set_yticks(y, [style.label(p) for p in pols])
    a1.invert_yaxis()
    save(fig, out, f"waits_{mix}_{cont}")


def fig_timeline(run, out, name="timeline_h1"):
    """H1 evidence: per-window worst response vs SLO, with batch progress in its own panel."""
    rq = run.requests
    t0, t1 = run.t_meas
    m = rq[rq.measured == 1]
    win = ((m.t_arrival_ns - t0) // 100_000_000).astype(int)
    worst = ((m.t_end_ns - m.t_arrival_ns) / 1e6).groupby(win).max()
    slo = run.cfg["mix_params"].get("slo_ms", 10)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(style.COL_W, 2.3), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    t = worst.index * 0.1
    a1.plot(t, worst.to_numpy(), color=style.color("default"), lw=0.9)
    bad = worst.to_numpy() > slo
    a1.plot(t[bad], worst.to_numpy()[bad], ls="none", marker="o", ms=2.5, color=style.CRITICAL,
            label=f"SLO miss ({bad.sum()} of {len(bad)} windows)")
    a1.axhline(slo, color=style.MUTED, lw=0.8, ls=(0, (3, 2)))
    a1.text(t.max(), slo, "SLO ", ha="right", va="bottom", fontsize=6.5, color=style.INK_2)
    plain_log(a1, "y")
    a1.legend(loc="upper right", fontsize=6)
    a1.set_ylabel("worst resp. (ms)")
    a1.set_title(f"{run.cfg['mix']}, {CONT_LABEL.get(run.cfg['contention'], '')}, B0 (seed {run.cfg['seed']})")
    ts = run.batch_ts
    if ts is not None:
        tot = ts.groupby("win").units.sum()
        a2.plot((tot.index - tot.index.min()) * 0.1, tot.to_numpy(), color=style.INK_2, lw=0.9)
    a2.set_ylabel("batch units\n/100 ms")
    a2.set_xlabel("time in measured window (s)")
    save(fig, out, name)


def fig_ablation(paired, out, cont="x2"):
    if paired is None:
        return
    p = paired[paired.contention == cont]
    pols = style.ordered(p.policy.unique())
    agg = p.groupby("policy")[["rt_p99_us_ratio", "batch_tput_ratio", "wait_batch_max_us_ratio"]].median().loc[pols]
    y = np.arange(len(pols))
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 0.25 * len(pols) + 0.7), sharey=True)
    for ax, col, title in zip(axes, agg.columns, ("interactive p99 / B0", "batch throughput / B0", "batch max wait / B0")):
        ax.barh(y, agg[col] - 1, left=1, height=0.62, color=[style.color(q) for q in pols], edgecolor="white", lw=0.8)
        ax.axvline(1, color=style.INK_2, lw=0.8)
        ax.set_title(title)
        # Value on the far side of the reference line, so it never collides with the tick labels.
        for k, v in enumerate(agg[col]):
            ax.text(1, k, f" {v:.2f}" if v < 1 else f"{v:.2f} ", va="center", ha="left" if v < 1 else "right",
                    fontsize=6, color=style.INK_2)
        lo, hi = min(agg[col].min(), 1), max(agg[col].max(), 1)
        pad = (hi - lo) * 0.25 + 0.01
        ax.set_xlim(lo - (pad if lo < 1 else 0.02), hi + (pad if hi > 1 else 0.02) + 0.04)
    axes[0].set_yticks(y, [style.label(q) for q in pols])
    axes[0].invert_yaxis()
    fig.suptitle("Ablations (median over mixes and seeds; paired with B0)", fontsize=7.5, y=1.02)
    save(fig, out, "ablation")


def fig_theta(paired, out, prefix="wbs-t", cont="x2"):
    if paired is None:
        return
    p = paired[(paired.contention == cont) & (paired.policy.str.startswith(prefix) | (paired.policy == "waitboost-slice"))]
    if p.empty:
        return
    fig, ax = plt.subplots(figsize=(style.COL_W, 2.0))
    for mix, g in p.groupby("mix"):
        th = g.policy.map(lambda s: 500 if s == "waitboost-slice" else int(s[len(prefix):]))
        g = g.assign(theta=th).sort_values("theta")
        ax.plot(g.batch_tput_ratio, g.rt_p99_us_ratio, marker="o", ms=3.5, lw=1.0,
                color=style.SLOTS[sorted(p.mix.unique()).index(mix)], label=mix)
        for _, r in g.iterrows():
            ax.annotate(f"{r.theta / 1000:g}", (r.batch_tput_ratio, r.rt_p99_us_ratio), xytext=(3, 2),
                        textcoords="offset points", fontsize=6, color=style.INK_2)
    ax.axhline(1, color=style.AXIS, lw=0.8)
    ax.axvline(1, color=style.AXIS, lw=0.8)
    ax.set_xlabel("batch throughput / B0")
    ax.set_ylabel("interactive p99 / B0")
    ax.set_title("WB-slice threshold sweep (labels: θ in ms)")
    ax.legend(loc="best")
    save(fig, out, "theta_sweep")


def fig_validation(threads, out):
    if threads is None or threads.empty:
        return
    fig, ax = plt.subplots(figsize=(style.COL_W, 2.2))
    for k, (role, g) in enumerate(threads.groupby("role")):
        ax.plot(g.schedstat_delay_ns / 1e6, g.ebpf_wait_ns / 1e6, ls="none", marker=style.MARKERS[k], ms=3,
                color=style.SLOTS[k], alpha=0.7, mec="none", label={"inter": "interactive", "batch": "batch"}[role])
    lim = [max(1e-3, threads.schedstat_delay_ns.min() / 1e6 * 0.8), threads.schedstat_delay_ns.max() / 1e6 * 1.2]
    ax.plot(lim, lim, color=style.INK_2, lw=0.8, ls=(0, (3, 2)))
    plain_log(ax, "x")
    plain_log(ax, "y")
    ax.set_xlabel("kernel schedstat run_delay (ms)")
    ax.set_ylabel("eBPF total run-queue wait (ms)")
    err = (threads.ebpf_wait_ns.sum() - threads.schedstat_delay_ns.sum()) / threads.schedstat_delay_ns.sum()
    ax.set_title(f"Per-thread validation, n={len(threads)}, aggregate error {err:+.2%}")
    ax.legend(loc="upper left")
    save(fig, out, "validation")


def fig_overhead(cells, out, mix="M1"):
    if cells is None:
        return
    c = cells[cells.mix == mix]
    order = ["default-nocollector", "default-noevents", "default"]
    names = {"default-nocollector": "no collector", "default-noevents": "maps only", "default": "maps + events"}
    conts = sorted(c.contention.unique())
    fig, axes = plt.subplots(1, 2, figsize=(style.COL_W, 1.75))
    for ax, col, title in zip(axes, ("batch_tput", "rt_p99_us"), ("batch throughput", "interactive p99")):
        for k, cfg in enumerate(order):
            vals = []
            for cont in conts:
                base = c[(c.contention == cont) & (c.policy == order[0])][col]
                v = c[(c.contention == cont) & (c.policy == cfg)][col]
                vals.append(float(v.iloc[0] / base.iloc[0]) if len(v) and len(base) else np.nan)
            ax.bar(np.arange(len(conts)) + (k - 1) * 0.27, vals, width=0.25, color=style.SLOTS[k], label=names[cfg],
                   edgecolor="white", lw=0.6)
        ax.axhline(1, color=style.INK_2, lw=0.8)
        ax.set_xticks(range(len(conts)), conts)
        ax.set_title(title)
    lo = np.nanmin([b.get_height() for a in axes for b in a.patches])
    for a in axes:
        a.set_ylim(min(0.9, lo - 0.02), None)
    axes[0].set_ylabel("relative to no collector")
    h, lab = axes[0].get_legend_handles_labels()
    fig.legend(h, lab, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.08), fontsize=6)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    save(fig, out, "overhead")


def fig_scaling(paired, out):
    if paired is None:
        return
    fig, ax = plt.subplots(figsize=(style.COL_W, 1.9))
    for pol in style.ordered(paired.policy.unique()):
        g = paired[paired.policy == pol].sort_values("cores")
        ax.errorbar(g.cores, g.rt_p99_us_ratio, yerr=[g.rt_p99_us_ratio - g.rt_p99_us_ratio_lo,
                                                      g.rt_p99_us_ratio_hi - g.rt_p99_us_ratio],
                    marker=style.marker(pol), color=style.color(pol), ms=4, lw=1.0, capsize=1.5, label=style.label(pol))
    ax.axhline(1, color=style.AXIS, lw=0.8)
    ax.set_xticks(sorted(paired.cores.unique()))
    ax.set_xlabel("experiment cores")
    ax.set_ylabel("interactive p99 / B0")
    ax.legend(ncol=2, fontsize=6)
    save(fig, out, "scaling")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", default=str(CODE / "results" / "summary"))
    ap.add_argument("--raw", default=str(CODE / "results" / "raw"))
    ap.add_argument("--out", default=str(CODE.parent / "report" / "figures"))
    ap.add_argument("--main")
    ap.add_argument("--heldout")
    ap.add_argument("--ablation")
    ap.add_argument("--overhead")
    ap.add_argument("--scaling")
    ap.add_argument("--timeline-run", help="run_id for the H1 timeline (default: M1_x4_default_c2_s<first seed>)")
    args = ap.parse_args()
    style.apply()
    S, R, O = Path(args.summary), Path(args.raw), Path(args.out)

    if args.main:
        cells, paired = read(S, args.main, "cells"), read(S, args.main, "paired")
        for mix in ("M1", "M2", "M3"):
            if (R / args.main).exists():
                fig_cdf(R / args.main, mix, O)
        fig_pareto(paired, O, "pareto_main")
        if cells is not None:
            loaded = [c for c in ("x4", "x2", "x1") if (cells.contention == c).any()]
            for mix in sorted(cells.mix.unique()):
                if loaded:  # heaviest contention present (x4 in the main grid)
                    fig_waits(cells, mix, loaded[0], O)
        runs = read(S, args.main, "runs")
        rid = args.timeline_run
        if rid is None and runs is not None:
            cand = runs[(runs.mix == "M1") & (runs.contention == "x4") & (runs.policy == "default")]
            rid = cand.sort_values("seed").run_id.iloc[0] if len(cand) else None
        if rid and (R / args.main / rid).exists():
            fig_timeline(Run(R / args.main / rid), O)
        fig_validation(read(S, args.main, "threads"), O)
    if args.heldout:
        fig_pareto(read(S, args.heldout, "paired"), O, "pareto_heldout", conts=("x1", "x2", "x4"))
    if args.ablation:
        pa = read(S, args.ablation, "paired")
        fig_ablation(pa[~pa.policy.str.match(r"wb[sn]-t")] if pa is not None else None, O)
        fig_theta(pa, O)
    if args.overhead:
        fig_overhead(read(S, args.overhead, "cells"), O)
    if args.scaling:
        fig_scaling(read(S, args.scaling, "paired"), O)


if __name__ == "__main__":
    main()
