# Team 10, Assignment A2

**eBPF-Based Detection and Mitigation of CPU Scheduling Unfairness Under Mixed Workloads**

Gokul Krishna Balaji (CS24B2053), Garikina Asish Preetam (CS24I1022), Bhyrapuneni Venkat Manoj (CS24B2052), Karra Sunith Vardhan Singh (CS24B2017)
Indian Institute of Information Technology, Design and Manufacturing, Kancheepuram

We measure, with eBPF, how long every thread waits on the Linux run queue, and use it to study and reduce CPU scheduling unfairness between short interactive tasks and CPU-bound batch tasks under Linux's EEVDF scheduler. EEVDF keeps long-run CPU shares fair but makes interactive p99 response time 1.7–4.0× slower in episodes. Per-task knobs (nice, EEVDF custom slices) and our WaitBoost policy driving them cannot fix this when the two classes run in separate cgroups. Class-level `cpu.weight` cuts interactive p99 by 42% at no measurable throughput cost.

## Contents

| Folder | What is in it |
|---|---|
| `latex_report/` | `report.pdf` (the six-page IEEE report), `main.tex`, `references.bib`, `IEEEtran.cls` and `IEEEtran.bst` (the IEEE template's class and style), `figures/` (the figures used), `other_latex_files/` (the tables included by `main.tex`) |
| `code/` | Complete source code, scripts, configurations, dependency list (`requirements.txt`) and the raw data of the reported run |
| `documentation/` | `reproducibility_guide.pdf` and `reproducibility_guide.md`: code organization, dependencies and versions, requirements, setup, configuration, execution commands, reproduction, output files and metrics |
| `supplementary/datasets/` | Why there is no external dataset and where the experiment data is |
| `supplementary/experimental_results/` | Summary CSVs of every batch, run logs, all result tables, and every number quoted in the report |
| `supplementary/additional_figures/` | Every figure the analysis produces, including those not in the report |
| `supplementary/other_supporting_materials/` | The hand-checkable trace validation (eBPF waits against an independent kernel trace) |

The code keeps the layout it was run with. In the standard structure's terms: source is in `code/workload/`, `code/bpf/`, `code/common/`, `code/harness/` and `code/analysis/`; scripts are `code/Makefile`, `code/tests/` and `code/setup/`; configurations are in `code/harness/configs/`.

## Quick start

On Ubuntu 26.04 (native, WSL2 or a VM), unpacking inside the Linux file system:

```bash
sudo apt install -y unzip
unzip Team_10_Assignment_A2.zip -d ~/A2 && cd ~/A2/code

# 1. Regenerate every number, table and figure in the report from the included raw data (no root, ~2 min)
make reproduce

# 2. Set up the machine, build and test (root, ~5 min)
sudo ./setup/provision.sh
make && make test

# 3. Re-run the whole study (root, ~8 h; keep the machine awake)
make golden
```

Step 1 needs only Python 3 with numpy, pandas, scipy and matplotlib (`code/requirements.txt`), zstd and tar. Full details are in `documentation/reproducibility_guide.pdf`.

## Compiling the report

The report uses the IEEE conference template (`IEEEtran`, conference mode). From `latex_report/`:

```bash
latexmk -pdf main.tex
```

or upload the `latex_report/` folder to Overleaf. It compiles to the six-page `report.pdf`.
