#!/usr/bin/env python3
"""Build the single submission archive Team_<NN>_Assignment_<A>.zip from the committed repository.

  python tools/make_submission.py --team 10 --assignment A2      -> submission/Team_10_Assignment_A2.zip

Everything comes from HEAD (the working tree must have no uncommitted changes to tracked files), so the
archive is exactly what was committed and tested. Unix file modes are kept, so the scripts stay executable
when the archive is unpacked with `unzip` on Linux. Layout follows the course's recommended structure:
README.md, latex_report/, code/, documentation/, supplementary/{datasets, experimental_results,
additional_figures, other_supporting_materials}/.
"""
import argparse
import io
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import zstandard

ROOT = Path(__file__).resolve().parents[1]
TS = "20261106-174432"
STORED = (".zst", ".png", ".pdf", ".zip", ".gz") + tuple(f".part{i:02d}" for i in range(10))


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def head_files():
    """{path: (mode, bytes)} for every file tracked at HEAD."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD"))) as t:
        for m in t.getmembers():
            if m.isfile():
                out[m.name] = (m.mode, t.extractfile(m).read())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--team", type=int, required=True)
    ap.add_argument("--assignment", required=True, choices=["A1", "A2", "A3", "A4"])
    args = ap.parse_args()
    if not 1 <= args.team <= 29:
        sys.exit("team number must be 01-29")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("commit or discard changes to tracked files first, so the archive matches HEAD")

    files = head_files()
    entries = []  # (archive path, mode, bytes)

    def add(arc, src=None, data=None, mode=0o644):
        if src is not None:
            mode, data = files[src]
        entries.append((arc, mode, data))

    # Top level
    add("README.md", "tools/submission/README.md")

    # latex_report/: only what main.tex uses; generated tables go to other_latex_files/
    tex = files["report/main.tex"][1].decode()
    figs = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", tex)
    tables = re.findall(r"\\input\{tables/([^}]+)\}", tex)
    assert len(figs) == 5 and len(tables) == 4, (figs, tables)
    tex = tex.replace(r"\input{tables/", r"\input{other_latex_files/")
    add("latex_report/main.tex", data=tex.encode())
    for name in ("references.bib", "IEEEtran.cls", "IEEEtran.bst"):
        add(f"latex_report/{name}", f"report/{name}")
    add("latex_report/report.pdf", "report/main.pdf")
    for f in figs:
        add(f"latex_report/{f}", f"report/{f}")
    for t in tables:
        add(f"latex_report/other_latex_files/{t}", f"report/tables/{t}")

    # code/: the whole tracked tree, modes kept
    for path in sorted(p for p in files if p.startswith("code/")):
        add(path, path)

    # documentation/
    add("documentation/reproducibility_guide.pdf", "docs/reproducibility_guide.pdf")
    add("documentation/reproducibility_guide.md", "docs/reproducibility_guide.md")

    # supplementary/
    for path in sorted(p for p in files if p.startswith("tools/submission/supplementary/")):
        add(path[len("tools/submission/"):], path)
    for path in sorted(p for p in files if p.startswith("code/results/summary/") and TS in p):
        add("supplementary/experimental_results/summary/" + Path(path).name, path)
    for path in sorted(p for p in files if re.match(r"report/tables/tab_\w+\.tex$", p)):
        add("supplementary/experimental_results/tables/" + Path(path).name, path)
    numbers = subprocess.run([sys.executable, "-B", str(ROOT / "code/analysis/paper_numbers.py"),
                              "--summary", str(ROOT / "code/results/summary"), "--ts", TS],
                             check=True, capture_output=True).stdout
    add("supplementary/experimental_results/paper_numbers.txt", data=numbers)
    for path in sorted(p for p in files if p.startswith("report/figures/") and p.endswith(".png")):
        add("supplementary/additional_figures/" + Path(path).name, path)
    archive = files[f"code/results/raw/validation_{TS}.tar.zst"][1]
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(archive)) as raw, tarfile.open(fileobj=raw, mode="r|") as t:
        for m in t:
            if m.isfile() and Path(m.name).name in ("validation_hand.md", "validation_waits.csv"):
                add("supplementary/other_supporting_materials/trace_validation/" + Path(m.name).name,
                    data=t.extractfile(m).read())

    names = [e[0] for e in entries]
    assert len(names) == len(set(names)), "duplicate archive paths"
    out = ROOT / "submission" / f"Team_{args.team:02d}_Assignment_{args.assignment}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        for arc, mode, data in entries:
            info = zipfile.ZipInfo(arc, date_time=(2026, 11, 15, 12, 0, 0))
            info.create_system = 3  # Unix, so unzip restores the permission bits
            info.external_attr = (0o100000 | (mode & 0o777)) << 16
            info.compress_type = zipfile.ZIP_STORED if arc.endswith(STORED) else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    print(f"wrote {out} ({out.stat().st_size / 2**20:.1f} MB, {len(entries)} files) from {git('rev-parse', '--short', 'HEAD').decode().strip()}")


if __name__ == "__main__":
    main()
