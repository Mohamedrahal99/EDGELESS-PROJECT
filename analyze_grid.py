#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Analysis of the variable-arrival grid (Sweep C of campaign.sh):

    elephant fraction {0.0, 0.5, 1.0} x N {5, 20} x three strategies x 5 reps

Every round is attributed to the node that served it (workflow -> ollama placement),
so latency can be related to per-node contention, GPU load and power.

    python3 analyze_grid.py [--root results/campaign] [--out results/campaign/plots_grid]

    # swapped layout (orin-20 fast, orin-21..24 at capacity 0.3):
    python3 analyze_grid.py --root results/campaign_swap --out results/campaign_swap/plots_grid \
        --capacity orin-21=0.3,orin-22=0.3,orin-23=0.3,orin-24=0.3

Outputs <out>/*.png|pdf figures and <out>/tables/*.csv.
"""

import argparse
import glob
import os
import re

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
from scipy import stats

FRACS = ["0.0", "0.5", "1.0"]
NS = [5, 20]
POLICIES = ["CapacityWeightedRandom", "Intelligent", "RandomNode"]
POL_LABEL = {"CapacityWeightedRandom": "CapacityWeightedRandom",
             "Intelligent": "Intelligent (CAP)", "RandomNode": "RandomNode"}
POL_COLOR = {"CapacityWeightedRandom": "#27917c", "Intelligent": "#2a78d6", "RandomNode": "#eb6834"}
CLASS_LS = {"mouse": "-", "elephant": "--"}
CLASS_MK = {"mouse": "o", "elephant": "s"}
MIX_LABEL = {"0.0": "all mice", "0.5": "50/50", "1.0": "all elephants"}
MIX_SHORT = {"0.0": "mice", "0.5": "50/50", "1.0": "eleph."}
PAPER = False             # --paper: drop figure titles (captions go in the paper)
INK, INK2, GRID = "#0b0b0b", "#52514e", "#d9d8d4"

# node UUID -> host, recovered by joining instances/fids/placement snapshots
NODE_HOST = {
    "571955b1-3001-4b74-9fcd-b7f916c8b305": "mohamed-1",
    "f289cd1a-0751-11f1-8aac-3c6d660fc501": "orin-20",
    "f289cd1a-0751-11f1-8aac-3c6d660fc502": "orin-21",
    "f289cd1a-0751-11f1-8aac-3c6d660fc503": "orin-22",
    "f289cd1a-0751-11f1-8aac-3c6d660fc504": "orin-23",
    "f289cd1a-0751-11f1-8aac-3c6d660fc505": "orin-24",
}
ORINS = ["orin-20", "orin-21", "orin-22", "orin-23", "orin-24"]
MAX_GAP_S = 30.0          # ignore telemetry gaps longer than this when integrating power
DURATION_S = 3600.0

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial"],
    "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 10.5,
    "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 9,
    "axes.edgecolor": INK2, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True, "lines.linewidth": 2,
    "figure.dpi": 150, "savefig.dpi": 300, "savefig.bbox": "tight",
    "legend.frameon": False,
})


# --------------------------------------------------------------------------
# loading
def run_files(root):
    for frac in FRACS:
        for pol in POLICIES:
            for n in NS:
                for rep in range(1, 6):
                    base = os.path.join(root, frac, f"var_{pol}_N{n}_r{rep}_")
                    if os.path.exists(base + "summary.json"):
                        yield frac, pol, n, rep, base


def load_placement(path):
    """workflow id -> serving node host (prefer explicit ollama placement if present)."""
    wf2host = {}
    with open(path) as f:
        for line in f:
            m = re.search(r"-> node (\S+) \(workflow ([0-9a-f-]+)", line)
            if m:
                wf2host.setdefault(m.group(2), NODE_HOST.get(m.group(1), m.group(1)[-4:]))
            m = re.search(r"resource ollama -> node (\S+) \(workflow ([0-9a-f-]+)", line)
            if m:
                wf2host[m.group(2)] = NODE_HOST.get(m.group(1), m.group(1)[-4:])
    return wf2host


def load_all(root):
    rounds, nodes, runs = [], [], []
    for frac, pol, n, rep, base in run_files(root):
        key = dict(frac=frac, policy=pol, N=n, rep=rep)
        r = pd.read_csv(base + "rounds.csv",
                        usecols=["session", "agent", "cycle", "wf_id", "agent_class", "round",
                                 "answer_tokens_est", "prompt_tokens_est", "latency_s",
                                 "timed_out", "attempts", "error", "t_send_unix"])
        wf2host = load_placement(base + "placement.txt")
        r["node"] = r["wf_id"].map(wf2host)
        for k, v in key.items():
            r[k] = v
        t0 = r["t_send_unix"].min()
        r["t_rel"] = r["t_send_unix"] - t0
        rounds.append(r)

        nd = pd.read_csv(base + "nodes.csv")
        for k, v in key.items():
            nd[k] = v
        nd["t_rel"] = nd["unix_ts"] - t0
        nodes.append(nd)

        runs.append(dict(key, base=base, t0=t0,
                         t1=(r["t_send_unix"] + r["latency_s"].fillna(0)).max(),
                         unmapped=int(r["node"].isna().sum())))
    R = pd.concat(rounds, ignore_index=True)
    R["ok"] = R["latency_s"].notna()
    R["failed"] = (R["timed_out"] != 0) & R["latency_s"].isna()
    R["cut"] = R["error"].fillna("") == "cut_at_deadline"
    return R, pd.concat(nodes, ignore_index=True), pd.DataFrame(runs)


# --------------------------------------------------------------------------
# metrics
def in_flight(df):
    """For each completed round: how many requests were in flight on the same node
    when it was sent (excluding itself). Measures the contention it ran into."""
    out = pd.Series(np.nan, index=df.index)
    ok = df[df["ok"] & df["node"].notna()]
    for _, g in ok.groupby(["frac", "policy", "N", "rep", "node"]):
        s = g["t_send_unix"].to_numpy()
        e = s + g["latency_s"].to_numpy()
        order_s, order_e = np.sort(s), np.sort(e)
        started = np.searchsorted(order_s, s, side="left")      # sent strictly before
        ended = np.searchsorted(order_e, s, side="right")       # finished by then
        out.loc[g.index] = started - ended
    return out


def energy_per_run(N, runs):
    """Orin energy over the run window (J), trapezoid on active power."""
    rows = []
    for _, run in runs.iterrows():
        sel = N[(N["frac"] == run["frac"]) & (N["policy"] == run["policy"])
                & (N["N"] == run["N"]) & (N["rep"] == run["rep"])
                & N["node"].isin(ORINS)]
        sel = sel[(sel["unix_ts"] >= run["t0"]) & (sel["unix_ts"] <= run["t1"])]
        tot = 0.0
        for _, g in sel.groupby("node"):
            g = g.sort_values("unix_ts")
            g = g[g["active_power_mw"] >= 0].drop_duplicates("unix_ts")
            t = g["unix_ts"].to_numpy(float)
            p = g["active_power_mw"].to_numpy(float) / 1000.0
            dt = np.diff(t)
            keep = dt <= MAX_GAP_S
            tot += float(np.sum(0.5 * (p[1:] + p[:-1])[keep] * dt[keep]))
        rows.append(dict(frac=run["frac"], policy=run["policy"], N=run["N"],
                         rep=run["rep"], energy_J=tot,
                         window_s=run["t1"] - run["t0"]))
    return pd.DataFrame(rows)


def per_run(R, E):
    g = R.groupby(["frac", "policy", "N", "rep"])
    df = pd.DataFrame({
        "completed": g["ok"].sum(),
        "failed": g["failed"].sum(),
        "retried": g["attempts"].apply(lambda a: int((a > 1).sum())),
        "out_tokens": g.apply(lambda x: x.loc[x["ok"], "answer_tokens_est"].sum()),
        "lat_mean": g["latency_s"].mean(),
        "lat_p50": g["latency_s"].median(),
        "lat_p95": g["latency_s"].quantile(0.95),
        "lat_p99": g["latency_s"].quantile(0.99),
    }).reset_index()
    for cls in ["mouse", "elephant"]:
        gc = R[R["agent_class"] == cls].groupby(["frac", "policy", "N", "rep"])["latency_s"]
        df = df.merge(pd.DataFrame({f"{cls}_p50": gc.median(), f"{cls}_p95": gc.quantile(0.95),
                                    f"{cls}_n": gc.count()}).reset_index(),
                      how="left", on=["frac", "policy", "N", "rep"])
    df = df.merge(E, on=["frac", "policy", "N", "rep"])
    df["rounds_per_h"] = df["completed"] / (DURATION_S / 3600.0)
    df["tok_per_s"] = df["out_tokens"] / DURATION_S
    df["J_per_round"] = df["energy_J"] / df["completed"]
    df["J_per_tok"] = df["energy_J"] / df["out_tokens"]
    df["avg_power_W"] = df["energy_J"] / df["window_s"]
    return df


def ci95(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return np.nan
    return stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))


def summarise(PR):
    metrics = ["completed", "failed", "retried", "tok_per_s", "lat_mean", "lat_p50", "lat_p95",
               "lat_p99", "mouse_p50", "mouse_p95", "elephant_p50", "elephant_p95",
               "avg_power_W", "J_per_round", "J_per_tok"]
    rows = []
    for (frac, pol, n), g in PR.groupby(["frac", "policy", "N"]):
        row = dict(frac=frac, policy=pol, N=n, reps=len(g))
        for m in metrics:
            row[m] = g[m].mean()
            row[m + "_ci95"] = ci95(g[m])
        rows.append(row)
    return pd.DataFrame(rows)


def paired(PR):
    """Per-rep strategy / CAP contrasts; each repetition uses the same arrival seed."""
    a = PR[PR["policy"] == "Intelligent"].set_index(["frac", "N", "rep"])
    rows = []
    for strategy in [p for p in POLICIES if p != "Intelligent"]:
        b = PR[PR["policy"] == strategy].set_index(["frac", "N", "rep"])
        for m, better in [("completed", "higher"), ("tok_per_s", "higher"), ("lat_p50", "lower"),
                          ("lat_p95", "lower"), ("J_per_tok", "lower")]:
            # Some campaign cells may contain only a subset of policies. Compare
            # only repetitions present for both policies, and leave unmatched
            # cells out of the paired test rather than failing on a missing key.
            matched = pd.concat([a[m].rename("cap"), b[m].rename("strategy")], axis=1).dropna()
            if matched.empty:
                continue
            matched["ratio"] = matched["strategy"] / matched["cap"]
            for (frac, n), g in matched.groupby(level=["frac", "N"]):
                d = g["strategy"] - g["cap"]
                p = stats.ttest_rel(g["strategy"], g["cap"]).pvalue if len(g) > 1 else np.nan
                rows.append(dict(strategy=strategy, metric=m, frac=frac, N=n,
                                 ratio_strategy_over_CAP=g["ratio"].mean(),
                                 ratio_min=g["ratio"].min(), ratio_max=g["ratio"].max(),
                                 mean_diff=d.mean(), paired_t_p=p, better=better))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# plotting helpers
def save(fig, out, name):
    if PAPER and fig._suptitle is not None:
        fig._suptitle.set_visible(False)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"))
    plt.close(fig)
    print("  wrote", name)


def policy_legend(ax, loc="upper left", extra=()):
    h = [Line2D([], [], color=POL_COLOR[p], lw=2.5, label=POL_LABEL[p]) for p in POLICIES]
    ax.legend(handles=h + list(extra), loc=loc)


def fig_legend(fig, extra=(), y=0.955):
    """Legend in its own row under the title, so it never sits on data."""
    h = [Line2D([], [], color=POL_COLOR[p], lw=6, label=POL_LABEL[p]) for p in POLICIES]
    fig.legend(handles=h + list(extra), loc="upper center", bbox_to_anchor=(0.5, y),
               ncol=len(h) + len(extra))


def plain_log(ax, axis="y"):
    """Log scale with plain tick labels (2, 5, 10, 100) instead of 2x10^0."""
    f = FuncFormatter(lambda v, _: f"{v:g}")
    a = ax.yaxis if axis == "y" else ax.xaxis
    a.set_major_formatter(f)
    a.set_minor_formatter(FuncFormatter(lambda v, _: f"{v:g}" if str(f"{v:g}")[0] in "25" else ""))


def grouped_dots(ax, PR, metric, n, ylabel, logy=False):
    """Bars = mean over reps, dots = individual reps, whisker = 95% CI."""
    x = np.arange(len(FRACS))
    w = 0.82 / len(POLICIES)
    for i, pol in enumerate(POLICIES):
        xs = x + (i - (len(POLICIES) - 1) / 2) * w
        means, cis = [], []
        for j, frac in enumerate(FRACS):
            v = PR[(PR["frac"] == frac) & (PR["policy"] == pol) & (PR["N"] == n)][metric]
            means.append(v.mean())
            cis.append(ci95(v))
            jitter = np.linspace(-0.08, 0.08, len(v))
            ax.scatter(xs[j] + jitter, v, s=12, color=INK2, zorder=3, linewidths=0)
        ax.bar(xs, means, width=w - 0.04, color=POL_COLOR[pol], alpha=0.85, zorder=2)
        ax.errorbar(xs, means, yerr=cis, fmt="none", ecolor=INK, elinewidth=1, capsize=3, zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels([MIX_LABEL[f] for f in FRACS])
    ax.set_ylabel(ylabel)
    ax.set_title(f"N = {n} agents")
    if logy:
        ax.set_yscale("log")
        plain_log(ax)


# --------------------------------------------------------------------------
# figures
def fig_throughput(PR, out):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.2))
    for c, n in enumerate(NS):
        grouped_dots(axes[0, c], PR, "completed", n, "completed rounds / hour")
        grouped_dots(axes[1, c], PR, "tok_per_s", n, "generated tokens / s (est.)")
    fig.suptitle("Throughput: rounds completed and tokens generated in the 1-hour run "
                 "(bar = mean, whisker = 95% CI)", y=1.0)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_legend(fig, extra=[Line2D([], [], marker="o", ls="", color=INK2, ms=5, label="one repetition")])
    save(fig, out, "fig01_throughput")


def fig_latency_ecdf(R, out):
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.4), sharex=True, sharey=True)
    for r, n in enumerate(NS):
        for c, frac in enumerate(FRACS):
            ax = axes[r, c]
            for pol in POLICIES:
                for cls in ["mouse", "elephant"]:
                    v = R[(R["frac"] == frac) & (R["N"] == n) & (R["policy"] == pol)
                          & (R["agent_class"] == cls) & R["ok"]]["latency_s"].sort_values()
                    if len(v) == 0:
                        continue
                    ax.plot(v, np.arange(1, len(v) + 1) / len(v), color=POL_COLOR[pol],
                            ls=CLASS_LS[cls], lw=1.8)
            ax.set_xscale("log")
            plain_log(ax, "x")
            ax.axhline(0.95, color=INK2, lw=0.8, ls=":")
            ax.set_title(f"N={n}, {MIX_LABEL[frac]}")
            if c == 0:
                ax.set_ylabel("fraction of rounds")
            if r == 1:
                ax.set_xlabel("round latency (s, log)")
    axes[0, 0].text(0.55, 0.955, "p95", color=INK2, fontsize=8, transform=axes[0, 0].get_yaxis_transform())
    extra = [Line2D([], [], color=INK2, ls=CLASS_LS[c], label=c) for c in ["mouse", "elephant"]]
    policy_legend(axes[0, 0], loc="lower right", extra=extra)
    fig.suptitle("Latency distribution per round (ECDF), split by agent class", y=1.0)
    fig.tight_layout()
    save(fig, out, "fig02_latency_ecdf")


def fig_latency_class(PR, out):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), sharex=True)
    x = np.arange(len(FRACS))
    for c, n in enumerate(NS):
        for r, q in enumerate(["p50", "p95"]):
            ax = axes[r, c]
            for pol in POLICIES:
                for cls in ["mouse", "elephant"]:
                    m, lo, hi = [], [], []
                    for frac in FRACS:
                        v = PR[(PR["frac"] == frac) & (PR["policy"] == pol) & (PR["N"] == n)][f"{cls}_{q}"]
                        med = v.median()
                        m.append(med)
                        lo.append(med - v.quantile(0.25) if len(v.dropna()) else np.nan)
                        hi.append(v.quantile(0.75) - med if len(v.dropna()) else np.nan)
                    ax.errorbar(x, m, yerr=[lo, hi], color=POL_COLOR[pol], ls=CLASS_LS[cls],
                                marker=CLASS_MK[cls], ms=7, capsize=3, lw=2,
                                mfc="white" if cls == "elephant" else POL_COLOR[pol])
            ax.set_yscale("log")
            plain_log(ax)
            ax.set_title(f"N = {n}: {q} latency")
            ax.set_ylabel(f"{q} latency (s, log)")
            ax.set_xticks(x)
            ax.set_xticklabels([MIX_LABEL[f] for f in FRACS])
    extra = [Line2D([], [], color=INK2, ls=CLASS_LS[c], marker=CLASS_MK[c],
                    mfc="white" if c == "elephant" else INK2, label=c) for c in ["mouse", "elephant"]]
    fig.suptitle("Per-class latency: median of the 5 reps, bars = IQR across reps", y=1.0)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_legend(fig, extra=extra)
    save(fig, out, "fig03_latency_by_class")


def fig_latency_time(R, out, n=20, bin_s=300):
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    for c, frac in enumerate(FRACS):
        ax = axes[c]
        for pol in POLICIES:
            d = R[(R["frac"] == frac) & (R["N"] == n) & (R["policy"] == pol) & R["ok"]].copy()
            d = d[d["t_rel"] < (DURATION_S // bin_s - 1) * bin_s]   # drop the partial last bin
            d["bin"] = (d["t_rel"] // bin_s) * bin_s + bin_s / 2
            g = d.groupby("bin")["latency_s"]
            ax.plot(g.median().index / 60, g.median(), color=POL_COLOR[pol], lw=2)
            ax.plot(g.quantile(0.95).index / 60, g.quantile(0.95), color=POL_COLOR[pol], lw=1.2, ls="--")
        ax.set_yscale("log")
        plain_log(ax)
        ax.set_title(f"N={n}, {MIX_LABEL[frac]}")
        ax.set_xlabel("time since first request (min)")
    axes[0].set_ylabel("latency (s, log)")
    extra = [Line2D([], [], color=INK2, lw=2, label="median"),
             Line2D([], [], color=INK2, lw=1.2, ls="--", label="p95")]
    fig.suptitle(f"Latency over the run, {bin_s // 60}-min bins pooled over 5 reps: "
                 "steady after warm-up", y=1.04)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig_legend(fig, extra=extra, y=0.93)
    save(fig, out, f"fig04_latency_over_time_N{n}")


def heat(ax, M, rows, cols, title, fmt, cmap="Greys", vmax=None):
    im = ax.imshow(M, aspect="auto", cmap=cmap, vmin=0, vmax=vmax or np.nanmax(M))
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels(cols, rotation=0)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows)
    ax.grid(False)
    lim = (vmax or np.nanmax(M)) * 0.6
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            if not np.isnan(M[i, j]):
                ax.text(j, i, fmt.format(M[i, j]), ha="center", va="center", fontsize=8,
                        color="white" if M[i, j] > lim else INK)
    ax.set_title(title)
    return im


def cell_cols():
    return [(frac, n) for n in NS for frac in FRACS]


def fig_gpu_heat(Nd, R, runs, out):
    """Mean GPU load and power per orin during the run window."""
    cols = cell_cols()
    fig, axes = plt.subplots(2, len(POLICIES), figsize=(6 * len(POLICIES), 6.6), sharey=True,
                             squeeze=False)
    mats = []
    for k, pol in enumerate(POLICIES):
        G = np.full((len(ORINS), len(cols)), np.nan)
        P = np.full_like(G, np.nan)
        for j, (frac, n) in enumerate(cols):
            sel = []
            for _, run in runs[(runs["frac"] == frac) & (runs["N"] == n) & (runs["policy"] == pol)].iterrows():
                s = Nd[(Nd["frac"] == frac) & (Nd["N"] == n) & (Nd["policy"] == pol) & (Nd["rep"] == run["rep"])
                       & (Nd["unix_ts"] >= run["t0"]) & (Nd["unix_ts"] <= run["t1"])]
                sel.append(s)
            s = pd.concat(sel) if sel else Nd.iloc[0:0]
            for i, h in enumerate(ORINS):
                x = s[s["node"] == h]
                G[i, j] = x.loc[x["gpu_load_perc"] >= 0, "gpu_load_perc"].mean()
                P[i, j] = x.loc[x["active_power_mw"] >= 0, "active_power_mw"].mean() / 1000
        labels = [f"N={n}\n{MIX_LABEL[f]}" for f, n in cols]
        heat(axes[0, k], G, ORINS, labels, f"{POL_LABEL[pol]}: mean GPU load (%)", "{:.0f}", vmax=100)
        mats.append((k, P, labels))
    vmax = max(np.nanmax(P) for _, P, _ in mats)
    for k, P, labels in mats:
        heat(axes[1, k], P, ORINS, labels, f"{POL_LABEL[POLICIES[k]]}: mean active power (W)",
             "{:.1f}", cmap="Purples", vmax=vmax)
    fig.tight_layout()
    save(fig, out, "fig05_node_gpu_power")


def fig_contention(R, out):
    """Mechanism: latency rises with requests in flight on the serving node."""
    d = R[R["ok"] & R["inflight"].notna()]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.9))
    # (a) latency vs in-flight, pooled over policies (it is the node's behaviour)
    ax = axes[0]
    for cls in ["mouse", "elephant"]:
        g = d[d["agent_class"] == cls].groupby(d["inflight"].clip(upper=12))["latency_s"]
        q = g.agg(["median", lambda x: x.quantile(0.25), lambda x: x.quantile(0.75), "count"])
        q.columns = ["med", "q1", "q3", "n"]
        q = q[q["n"] >= 30]
        ax.plot(q.index, q["med"], color=INK, ls=CLASS_LS[cls], marker=CLASS_MK[cls], ms=5,
                mfc="white" if cls == "elephant" else INK, label=cls)
        ax.fill_between(q.index, q["q1"], q["q3"], color=INK2, alpha=0.12, lw=0)
    ax.set_xlabel("other requests in flight on the node at send (12 = 12+)")
    ax.set_ylabel("latency (s): median, IQR band")
    ax.set_title("Node service curve (all runs pooled)")
    ax.legend(loc="upper left")
    # (b) tail of the contention each round met: P(in-flight >= k), per policy
    for k, n in enumerate(NS):
        ax = axes[1 + k]
        ks = np.arange(0, 21)
        for pol in POLICIES:
            v = d[(d["policy"] == pol) & (d["N"] == n)]["inflight"].to_numpy()
            ax.plot(ks, [(v >= x).mean() for x in ks], color=POL_COLOR[pol], marker="o", ms=4)
        ax.set_yscale("log")
        plain_log(ax)
        ax.set_xlabel("k = other requests in flight on the node")
        ax.set_ylabel("fraction of rounds with >= k")
        ax.set_xticks(range(0, 21, 2))
        ax.set_title(f"Contention met per round, N={n}\n(all mixes pooled)")
    axes[0].set_title("Node service curve\n(all runs pooled)")
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig_legend(fig, y=0.97)
    save(fig, out, "contention")


def fig_contention_by_class(R, out):
    """Figure 6 style contention tails for elephants and mice at N=5 and N=20."""
    d = R[R["ok"] & R["inflight"].notna()]
    # Use the pure-class runs so each plot reports contention experienced by
    # elephants or mice without the other class in the workload.
    for frac, cls, filename in (("1.0", "elephant", "contention_elephant"),
                                ("0.0", "mouse", "contention_mice")):
        fig, axes = plt.subplots(1, len(NS), figsize=(11.6, 4.0), sharey=True)
        for ax, n in zip(axes, NS):
            subset = d[(d["frac"] == frac) & (d["agent_class"] == cls) & (d["N"] == n)]
            # Match fig_contention: k=20 represents 20 or more requests.
            ks = np.arange(0, 21)
            for pol in POLICIES:
                v = subset[subset["policy"] == pol]["inflight"].to_numpy()
                if len(v):
                    tail = np.array([(v >= k).mean() for k in ks])
                    # A log axis cannot show zero. Keep each curve through its last
                    # nonzero point so the tail remains readable.
                    nz = np.flatnonzero(tail > 0)
                    if len(nz):
                        end = nz[-1] + 1
                        ax.plot(ks[:end], tail[:end], color=POL_COLOR[pol],
                                marker="o", ms=4, label=POL_LABEL[pol])
            ax.set_yscale("log")
            plain_log(ax)
            ax.set_xlabel("k = other requests in flight on the node (20 = 20+)")
            ax.set_title(f"N={n}")
            ax.set_xlim(0, 20)
            ax.set_xticks(range(0, 21, 2))
        axes[0].set_ylabel("fraction of rounds with ≥ k in flight")
        fig.suptitle(f"Contention met per round: {cls} (elephant fraction {frac})")
        fig.legend([Line2D([], [], color=POL_COLOR[p], marker="o", ms=4, label=POL_LABEL[p])
                    for p in POLICIES], [POL_LABEL[p] for p in POLICIES],
                   loc="upper center", bbox_to_anchor=(0.5, 0.91), ncol=len(POLICIES))
        fig.tight_layout(rect=(0, 0, 1, 0.82))
        save(fig, out, filename)


def fig_energy(PR, out):
    fig, axes = plt.subplots(2, 2, figsize=(10, 7.2))
    for c, n in enumerate(NS):
        grouped_dots(axes[0, c], PR, "avg_power_W", n, "mean active power, 5 orins (W)")
        grouped_dots(axes[1, c], PR, "J_per_tok", n, "energy per generated token (J, est. tokens)")
    fig.suptitle("Energy: cluster power draw and energy efficiency (GPU nodes only)", y=1.0)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_legend(fig)
    save(fig, out, "fig07_energy")


def fig_paired(PR, out):
    """Per-rep strategy / CAP ratios on the same arrival seed."""
    a = PR[PR["policy"] == "Intelligent"].set_index(["frac", "N", "rep"])
    metrics = [("completed", "throughput\n(rounds)"), ("lat_p50", "p50 latency"),
               ("lat_p95", "p95 latency"), ("J_per_tok", "energy / token")]
    fig, axes = plt.subplots(1, len(metrics), figsize=(13, 3.8), sharey=False)
    for k, (m, lab) in enumerate(metrics):
        ax = axes[k]
        cols = cell_cols()
        competitors = [p for p in POLICIES if p != "Intelligent"]
        for k, strategy in enumerate(competitors):
            b = PR[PR["policy"] == strategy].set_index(["frac", "N", "rep"])
            ratio = (b[m] / a[m]).rename(0).reset_index()
            offset = (k - (len(competitors) - 1) / 2) * 0.28
            for j, (frac, n) in enumerate(cols):
                v = ratio[(ratio["frac"] == frac) & (ratio["N"] == n)][0]
                ax.scatter(np.full(len(v), j + offset) + np.linspace(-0.07, 0.07, len(v)), v,
                           s=14, color=POL_COLOR[strategy], zorder=3)
                ax.plot([j + offset - 0.1, j + offset + 0.1], [v.mean()] * 2,
                        color=POL_COLOR[strategy], lw=2.5)
        ax.axhline(1, color=INK2, lw=1, ls=":")
        if m == "lat_p95":          # spans 1-14x; the other ratios fit a linear axis
            ax.set_yscale("log")
            plain_log(ax)
        ax.set_xticks(range(len(cols)))
        ax.set_xticklabels([f"N={n}\n{MIX_SHORT[f]}" for f, n in cols], fontsize=8)
        ax.set_title(f"Strategy / CAP:\n{lab}")
    axes[0].set_ylabel("ratio; 1 = equal (p95 panel: log)")
    fig_legend(fig)
    fig.suptitle("Paired comparison: same arrival seed under both policies (dot = rep, bar = mean)", y=1.03)
    fig.tight_layout()
    save(fig, out, "fig08_paired_ratios")


def fig_node_latency(R, out, n=20):
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    for c, frac in enumerate(FRACS):
        ax = axes[c]
        for k, pol in enumerate(POLICIES):
            data = [R[(R["frac"] == frac) & (R["N"] == n) & (R["policy"] == pol) & R["ok"]
                      & (R["node"] == h)]["latency_s"].to_numpy() for h in ORINS]
            pos = np.arange(len(ORINS)) + (k - (len(POLICIES) - 1) / 2) * 0.25
            bp = ax.boxplot([x if len(x) else [np.nan] for x in data], positions=pos, widths=0.22,
                            whis=(5, 95), showfliers=False, patch_artist=True)
            for patch in bp["boxes"]:
                patch.set(facecolor=POL_COLOR[pol], alpha=0.75, edgecolor=INK2)
            for el in ["whiskers", "caps", "medians"]:
                for line in bp[el]:
                    line.set(color=INK2 if el != "medians" else INK, lw=1.2)
        ax.set_xticks(range(len(ORINS)))
        ax.set_xticklabels(ORINS)
        ax.set_yscale("log")
        plain_log(ax)
        ax.set_title(f"N={n}, {MIX_LABEL[frac]}")
    axes[0].set_ylabel("latency (s, log); box IQR, whiskers p5-p95")
    fig.suptitle(f"Latency per serving node, N={n}: {low_capacity_txt()}, {others_txt()}", y=1.04)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig_legend(fig, y=0.93)
    save(fig, out, f"fig09_latency_per_node_N{n}")


# --------------------------------------------------------------------------
# CAP's queueing model, re-implemented from edgeless_orc/src/intelligent.rs
# (predict_on, Params::default) so measured latency can be checked against it.
CAP_SERVICE = {"mouse": 0.95, "elephant": 5.44}   # s per round on a capacity-1.0 node
CAP_THINK = 18.0
CAP_CV = 0.30
NODE_CAPACITY = {"orin-20": 0.30}                  # node label; unlabelled nodes = 1.0 (--capacity)
SLOW_S = 30.0                                      # "slow round" threshold for fig12


def cap(host):
    return NODE_CAPACITY.get(host, 1.0)


def cap_txt(host):
    return f"{host} (capacity {cap(host):.2f})"


def others_txt():
    """Label for the full-capacity hosts as a group."""
    hosts = [h for h in ORINS if cap(h) >= 1.0]
    cs = sorted({cap(h) for h in hosts})
    c = f"{cs[0]:.2f}" if len(cs) == 1 else "/".join(f"{x:.2f}" for x in cs)
    return f"{','.join(hosts)} (capacity {c})"


def low_capacity_hosts():
    return [h for h in ORINS if cap(h) < 1.0]


def low_capacity_txt():
    hosts = low_capacity_hosts()
    return f"{','.join(hosts)} (capacity {','.join(f'{cap(h):.2f}' for h in hosts)})"


def parse_capacity(s):
    """'orin-21=0.3,orin-22=0.3' -> dict; hosts not listed are 1.0."""
    out = {}
    for kv in filter(None, (x.strip() for x in s.split(","))):
        h, v = kv.split("=")
        out[h.strip()] = float(v)
    return out


def mva_response(classes, capacity, cv=CAP_CV, iters=1000):
    """Predicted response time (s) of each agent on one single-slot FCFS node."""
    svc = np.array([CAP_SERVICE[c] for c in classes]) / max(capacity, 1e-6)
    resid = (1 + cv * cv) / 2
    r = svc.copy()
    for _ in range(iters):
        x = 1.0 / (CAP_THINK + r)
        u, q = x * svc, x * r
        delta = 0.0
        for j in range(len(r)):
            m = np.arange(len(r)) != j
            wait = np.sum(u[m] * svc[m] * resid + np.maximum(q[m] - u[m], 0) * svc[m])
            nxt = 0.5 * r[j] + 0.5 * (svc[j] + wait)
            delta = max(delta, abs(nxt - r[j]))
            r[j] = nxt
        if delta < 1e-9:
            break
    return r


def model_check(R):
    """Per workflow (one 12-round conversation on one node): measured mean slowdown vs
    the model's prediction for the population it actually shared the node with.
    Population at a round = workflows on that node whose lifetime (first send ->
    last answer) covers the round's send time; thinking agents count, as in MVA."""
    cache = {}

    def pred(node, n_m, n_e, own):
        key = (node, n_m, n_e, own)
        if key not in cache:
            cls = ["mouse"] * n_m + ["elephant"] * n_e
            cls.remove(own)
            cls.append(own)                         # own agent last
            cache[key] = mva_response(cls, NODE_CAPACITY.get(node, 1.0))[-1]
        return cache[key]

    d = R[R["ok"] & R["node"].notna()].copy()
    d["t_end"] = d["t_send_unix"] + d["latency_s"]
    rows = []
    for (frac, pol, n, rep, node), g in d.groupby(["frac", "policy", "N", "rep", "node"]):
        life = g.groupby("wf_id").agg(t0=("t_send_unix", "min"), t1=("t_end", "max"),
                                      cls=("agent_class", "first"))
        t0, t1 = life["t0"].to_numpy(), life["t1"].to_numpy()
        is_e = (life["cls"] == "elephant").to_numpy()
        for wf, w in g.groupby("wf_id"):
            p = []
            for t in w["t_send_unix"].to_numpy():
                live = (t0 <= t) & (t1 >= t)
                p.append(pred(node, int((live & ~is_e).sum()), int((live & is_e).sum()),
                              w["agent_class"].iat[0]))
            cls = w["agent_class"].iat[0]
            rows.append(dict(frac=frac, policy=pol, N=n, rep=rep, node=node, wf_id=wf, cls=cls,
                             rounds=len(w), pred_s=np.mean(p), meas_s=w["latency_s"].mean(),
                             pred_slow=np.mean(p) / CAP_SERVICE[cls],
                             meas_slow=w["latency_s"].mean() / CAP_SERVICE[cls]))
    M = pd.DataFrame(rows)
    M["rel_err"] = (M["pred_s"] - M["meas_s"]) / M["meas_s"]
    return M


def fig_model_check(M, out):
    """fig10: is the queueing model CAP decides with accurate on this testbed?"""
    M = M[M["rounds"] >= 6]                         # drop conversations cut at the deadline
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    lo, hi = 0.5, max(M["pred_slow"].max(), M["meas_slow"].max()) * 1.3
    for k, cls in enumerate(["mouse", "elephant"]):
        ax = axes[k]
        for pol in POLICIES:
            g = M[(M["cls"] == cls) & (M["policy"] == pol)]
            lowcap = g["node"].isin(low_capacity_hosts())
            ax.scatter(g.loc[~lowcap, "pred_slow"], g.loc[~lowcap, "meas_slow"], s=9, alpha=0.35,
                       color=POL_COLOR[pol], lw=0)
            ax.scatter(g.loc[lowcap, "pred_slow"], g.loc[lowcap, "meas_slow"], s=22, alpha=0.6,
                       facecolor="none", edgecolor=POL_COLOR[pol], lw=0.9)
        ax.plot([lo, hi], [lo, hi], color=INK, lw=1.2)
        for f in (0.5, 2.0):
            ax.plot([lo, hi], [lo * f, hi * f], color=INK2, lw=0.8, ls=":")
        ax.set_xscale("log"); ax.set_yscale("log")
        plain_log(ax, "x"); plain_log(ax, "y")
        ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
        g = M[M["cls"] == cls]
        e = g["rel_err"]
        ax.text(0.97, 0.04, f"{len(g)} conversations\nmedian error {np.median(e) * 100:+.0f}%\n"
                f"median |error| {np.median(np.abs(e)) * 100:.0f}%",
                transform=ax.transAxes, ha="right", va="bottom", fontsize=8.5, color=INK2)
        ax.set_title(f"{cls} agents")
        ax.set_xlabel("predicted slowdown (model, given co-located agents)")
    axes[0].set_ylabel("measured slowdown\n(mean latency / alone on a full-speed node)")
    extra = [Line2D([], [], color=INK, lw=1.2, label="exact"),
             Line2D([], [], color=INK2, lw=0.8, ls=":", label="2x off"),
             Line2D([], [], marker="o", ls="", mfc="none", mec=INK2, ms=6,
                    label=f"on {low_capacity_txt()}")]
    fig.suptitle("CAP's queueing model vs measurement, one point per conversation (all runs)", y=1.02)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig_legend(fig, extra=extra, y=0.95)
    save(fig, out, "fig10_model_vs_measured")


def lanes(intervals):
    """Greedy interval packing: lane index per (start, end), in input order."""
    order = np.argsort([s for s, _ in intervals])
    ends, out = [], [0] * len(intervals)
    for i in order:
        s, e = intervals[i]
        for k, last in enumerate(ends):
            if last <= s:
                ends[k] = e
                out[i] = k
                break
        else:
            ends.append(e)
            out[i] = len(ends) - 1
    return out


def fig_placement_timeline(R, out, frac="0.5", n=20, rep=1):
    """fig11: where each conversation ran, over the hour, under both policies."""
    d = R[(R["frac"] == frac) & (R["N"] == n) & (R["rep"] == rep) & R["node"].notna()].copy()
    d["t_end"] = d["t_rel"] + d["latency_s"].fillna(0)
    fig, axes = plt.subplots(len(POLICIES), 1, figsize=(12, 3.8 * len(POLICIES)), sharex=True,
                             squeeze=False)
    axes = axes[:, 0]
    col = {"mouse": "#b9b8b3", "elephant": INK}
    for k, pol in enumerate(POLICIES):
        ax = axes[k]
        g = d[d["policy"] == pol]
        wf = g.groupby("wf_id").agg(node=("node", "first"), cls=("agent_class", "first"),
                                    t0=("t_rel", "min"), t1=("t_end", "max"))
        y, yt, yl = 0.0, [], []
        for h in ORINS[::-1]:
            w = wf[wf["node"] == h]
            ln = lanes(list(zip(w["t0"] / 60, w["t1"] / 60))) if len(w) else []
            nl = max(ln) + 1 if ln else 1
            for (_, row), l in zip(w.iterrows(), ln):
                ax.barh(y + l + 0.5, (row["t1"] - row["t0"]) / 60, left=row["t0"] / 60, height=0.78,
                        color=col[row["cls"]], edgecolor="white", lw=0.4)
            yt.append(y + nl / 2)
            yl.append(f"{h}\n({len(w)} conv.)")
            y += nl
            ax.axhline(y + 0.25, color=INK2, lw=0.8)
            y += 0.5
        ax.set_yticks(yt)
        ax.set_yticklabels(yl, fontsize=8.5)
        ax.set_ylim(0, y)
        ax.grid(axis="y", visible=False)
        ax.set_title(POL_LABEL[pol], color=POL_COLOR[pol], loc="left", fontweight="bold")
    axes[1].set_xlabel("time since first request (min); one bar = one 12-round conversation")
    axes[1].set_xlim(0, DURATION_S / 60)
    h = [plt.Rectangle((0, 0), 1, 1, color=col[c], label=c) for c in ["mouse", "elephant"]]
    fig.legend(handles=h, loc="upper center", bbox_to_anchor=(0.5, 0.955), ncol=2)
    fig.suptitle(f"Placement over time, N={n}, {MIX_LABEL[frac]}, repetition {rep} "
                 "(same arrivals for both policies)", y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    save(fig, out, f"fig11_placement_timeline_N{n}_{MIX_SHORT[frac].strip('.').replace('/', '-')}_r{rep}")


def fig_slow_rounds(R, out):
    """fig12: which node the slow rounds (> SLOW_S, or timed out) come from."""
    d = R[R["node"].notna()].copy()
    d["slow"] = (d["latency_s"] > SLOW_S) | d["failed"]
    cnt = (d[d["slow"]].assign(lowcap=lambda x: x["node"].isin(low_capacity_hosts()))
           .groupby(["frac", "N", "policy", "lowcap"]).size() / 5).rename("per_run")
    tot = d.groupby(["frac", "N", "policy"]).size() / 5
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    seg = [(True, low_capacity_txt(), INK), (False, others_txt(), "#b9b8b3")]
    for c, n in enumerate(NS):
        ax = axes[c]
        xt, xl = [], []
        for j, frac in enumerate(FRACS):
            for k, pol in enumerate(POLICIES):
                x = j * 2.6 + k * 1.0
                base = 0.0
                for lowcap, lab, color in seg:
                    v = cnt.get((frac, n, pol, lowcap), 0.0)
                    ax.bar(x, v, bottom=base, width=0.82, color=color, edgecolor="white", lw=1.5)
                    base += v
                pct = 100 * base / tot.get((frac, n, pol), np.nan)
                ptxt = "<0.1%" if pct < 0.05 else f"{pct:.1f}%"
                ax.text(x, base, f"{base:.0f}\n({ptxt})" if base else "0", ha="center",
                        va="bottom", fontsize=8, color=INK2)
                xt.append(x)
                xl.append({"Intelligent": "CAP", "RandomNode": "RN",
                           "CapacityWeightedRandom": "CWR"}[pol])
            ax.text(j * 2.6 + 0.5, -0.16, MIX_LABEL[frac], transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=9)
        ax.set_xticks(xt)
        ax.set_xticklabels(xl, fontsize=8.5)
        ax.tick_params(axis="x", length=0)
        ax.grid(axis="x", visible=False)
        ax.margins(y=0.18)
        ax.set_title(f"N={n}")
    axes[0].set_ylabel(f"rounds > {SLOW_S:.0f} s or timed out, per run\n(mean of 5 reps; % of all rounds)")
    h = [plt.Rectangle((0, 0), 1, 1, color=color, label=lab) for _, lab, color in seg]
    fig.legend(handles=h, loc="upper center", bbox_to_anchor=(0.5, 0.955), ncol=2)
    fig.suptitle("Where slow rounds come from (CAP = Intelligent, RN = RandomNode, CWR = CapacityWeightedRandom)", y=0.99)
    fig.tight_layout(rect=(0, 0.02, 1, 0.92))
    save(fig, out, "fig12_slow_rounds_by_node")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="results/campaign")
    ap.add_argument("--out", default="results/campaign/plots_grid")
    ap.add_argument("--paper", action="store_true", help="no figure titles")
    ap.add_argument("--capacity", default="orin-20=0.30",
                    help="node capacity labels used by the runs, e.g. "
                         "'orin-21=0.3,orin-22=0.3,orin-23=0.3,orin-24=0.3' for the swapped "
                         "layout; unlisted nodes are 1.0 (default: original layout)")
    a = ap.parse_args()
    global PAPER, NODE_CAPACITY
    PAPER = a.paper
    NODE_CAPACITY = parse_capacity(a.capacity)
    print("capacities:", {h: cap(h) for h in ORINS})
    os.makedirs(os.path.join(a.out, "tables"), exist_ok=True)

    print("loading ...")
    R, Nd, runs = load_all(a.root)
    print(f"  {len(runs)} runs, {len(R)} rounds, unmapped rounds: {int(runs['unmapped'].sum())}")
    R["inflight"] = in_flight(R)
    E = energy_per_run(Nd, runs)
    PR = per_run(R, E)
    S = summarise(PR)
    P = paired(PR)

    T = os.path.join(a.out, "tables")
    PR.to_csv(os.path.join(T, "per_run.csv"), index=False)
    S.to_csv(os.path.join(T, "per_cell_mean_ci95.csv"), index=False)
    P.to_csv(os.path.join(T, "paired_strategies_vs_CAP.csv"), index=False)

    print("plotting ...")
    fig_throughput(PR, a.out)
    fig_latency_ecdf(R, a.out)
    fig_latency_class(PR, a.out)
    fig_latency_time(R, a.out, n=20)
    fig_latency_time(R, a.out, n=5)
    fig_gpu_heat(Nd, R, runs, a.out)
    fig_contention(R, a.out)
    fig_contention_by_class(R, a.out)
    fig_energy(PR, a.out)
    fig_paired(PR, a.out)
    fig_node_latency(R, a.out, n=20)
    fig_node_latency(R, a.out, n=5)
    M = model_check(R)
    M.to_csv(os.path.join(T, "model_vs_measured.csv"), index=False)
    fig_model_check(M, a.out)
    for frac, n in [("0.5", 20), ("0.5", 5)]:
        fig_placement_timeline(R, a.out, frac=frac, n=n, rep=1)
    fig_slow_rounds(R, a.out)

    # node share table
    share = (R[R["ok"]].groupby(["frac", "N", "policy"])["node"].value_counts(normalize=True)
             .unstack().reindex(columns=ORINS) * 100).round(1)
    share.to_csv(os.path.join(T, "node_share_pct.csv"))
    infl = R[R["ok"]].groupby(["frac", "N", "policy"])["inflight"].agg(["mean", "median", lambda x: x.quantile(.95)])
    infl.columns = ["inflight_mean", "inflight_p50", "inflight_p95"]
    infl.to_csv(os.path.join(T, "inflight.csv"))
    print("done ->", a.out)


if __name__ == "__main__":
    main()
