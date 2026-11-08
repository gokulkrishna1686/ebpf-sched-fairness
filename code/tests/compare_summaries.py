#!/usr/bin/env python3
"""Compare recomputed summary CSVs with the committed ones, column by column.

  python3 tests/compare_summaries.py <recomputed_dir> results/summary <timestamp>

Exit status 0 means every table matches. Bootstrap intervals are seeded, but a different NumPy
version may change their last digits; the versions used for the paper are in the guide.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BATCHES = ("tuning", "main", "heldout", "ablation", "scaling", "overhead")
KEYS = {"runs": ["run_id"], "cells": ["mix", "contention", "cores", "policy"],
        "paired": ["mix", "contention", "cores", "policy"], "threads": ["run_id", "tid", "role"]}
TOLERANCE = 1e-6  # relative


def compare(new_csv, old_csv, key):
    """Return (number of common rows, rows found in one file only, names of differing columns)."""
    a, c = pd.read_csv(new_csv), pd.read_csv(old_csv)
    k = [x for x in key if x in a and x in c]
    a, c = a.set_index(k).sort_index(), c.set_index(k).sort_index()
    common = a.index.intersection(c.index)
    one_sided = len(a.index.symmetric_difference(c.index))
    differing = []
    for col in a.columns.intersection(c.columns):
        if not (pd.api.types.is_numeric_dtype(a[col]) and pd.api.types.is_numeric_dtype(c[col])):
            continue
        x, y = a.loc[common, col].to_numpy(float), c.loc[common, col].to_numpy(float)
        with np.errstate(invalid="ignore", divide="ignore"):
            rel = np.abs(x - y) / np.maximum(np.maximum(np.abs(x), np.abs(y)), 1e-12)
        rel[np.isnan(x) & np.isnan(y)] = 0
        rel[np.isnan(rel)] = 1  # NaN on one side only
        if len(rel) and rel.max() > TOLERANCE:
            differing.append(f"{col} ({rel.max():.2g})")
    return len(common), one_sided, differing


def main():
    new, old, ts = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    bad = 0
    for batch in BATCHES:
        for kind, key in KEYS.items():
            pn, po = new / f"{batch}_{ts}_{kind}.csv", old / f"{batch}_{ts}_{kind}.csv"
            if not (pn.exists() and po.exists()):
                print(f"DIFF  {batch}/{kind}: file missing")
                bad += 1
                continue
            rows, one_sided, differing = compare(pn, po, key)
            ok = not one_sided and not differing
            bad += not ok
            print(f"{'same' if ok else 'DIFF'}  {batch}/{kind}: {rows} rows"
                  + (f", {one_sided} rows in only one file" if one_sided else "")
                  + (f", differing columns: {', '.join(differing[:8])}" if differing else ""))
    print(f"\n{bad} table(s) differ" if bad else "\nAll tables match the committed results.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
