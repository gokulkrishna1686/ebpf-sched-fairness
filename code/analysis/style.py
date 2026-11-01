"""Figure style for the paper (IEEE two-column, static PDF, light background).

Colors follow the entity, never its rank: each policy owns one categorical slot in a fixed
order (validated: adjacent CVD dE >= 9.1, normal-vision dE >= 19.6). Three slots are below 3:1
contrast on white, so every chart also carries marker shapes / direct labels and the paper
repeats the numbers in tables.
"""
import matplotlib as mpl

SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
CRITICAL = "#d03b3b"  # status color: always paired with a text label, never a series color

POLICY_ORDER = ["default", "nice", "cgweight", "slice", "waitboost-nice", "waitboost-slice", "waitboost-cg"]
POLICY_LABEL = {
    "default": "B0 default", "nice": "B1 nice", "cgweight": "B2 cg-weight", "slice": "B3 slice",
    "waitboost-nice": "WB-nice", "waitboost-slice": "WB-slice", "waitboost-cg": "WB-cgroup",
}
MARKERS = ["o", "s", "D", "^", "P", "*", "X", "v"]

COL_W = 3.5    # inches, IEEE single column
PAGE_W = 7.16  # inches, IEEE double column


def color(policy):
    """Variants (e.g. waitboost-slice-noaging, wbs-t1000) inherit their parent's slot."""
    return SLOTS[_slot(policy) % len(SLOTS)]


def marker(policy):
    return MARKERS[_slot(policy) % len(MARKERS)]


def label(policy):
    if policy in POLICY_LABEL:
        return POLICY_LABEL[policy]
    for parent in sorted(POLICY_LABEL, key=len, reverse=True):
        if policy.startswith(parent + "-"):
            return POLICY_LABEL[parent] + policy[len(parent):]
    if policy.startswith("wbs-t"):
        return f"WB-slice θ={policy[5:]}µs"
    if policy.startswith("wbn-t"):
        return f"WB-nice θ={policy[5:]}µs"
    return policy


def _slot(policy):
    if policy in POLICY_ORDER:
        return POLICY_ORDER.index(policy)
    if policy.startswith("wbs-"):
        return POLICY_ORDER.index("waitboost-slice")
    if policy.startswith("wbn-"):
        return POLICY_ORDER.index("waitboost-nice")
    for parent in sorted(POLICY_ORDER, key=len, reverse=True):
        if policy.startswith(parent):
            return POLICY_ORDER.index(parent)
    return len(POLICY_ORDER)


def ordered(policies):
    """Policies in the fixed legend order, unknown ones last (alphabetically)."""
    return sorted(policies, key=lambda p: (_slot(p), p))


def apply():
    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
        "font.size": 7.5,
        "axes.titlesize": 8,
        "axes.labelsize": 7.5,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 6.8,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": INK_2,
        "axes.titlecolor": INK,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.5,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "lines.linewidth": 1.2,
        "lines.markersize": 4.5,
        "legend.frameon": False,
        "figure.dpi": 150,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
        "pdf.fonttype": 42,  # embed TrueType: IEEE PDF eXpress rejects Type 3 fonts
        "ps.fonttype": 42,
    })
