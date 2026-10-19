#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
    python3 analyse_compare.py --campaign results/campaign --out results/compare

Everything is written to --out:
    REPORT.md                 verdict, tables, how to read the figures
    runs.csv / cells.csv / stats.csv
    fig01_ecdf_N<n>.png       latency ECDFs: all / mice / elephants, per workload
    fig02_percentiles.png     P50/P90/P99 per workload and load
    fig03_class_latency.png   mouse and elephant median and P99
    fig04_makespan.png        makespan with per-repetition points and p-values
    fig05_contention.png      slowdown distributions (head-of-line blocking)
    fig06_slo.png             share of rounds that exceed a slowdown budget
    fig07_energy.png          energy per token, total energy, throughput
    fig08_node_balance.png    per-node GPU load and imbalance
    fig09_node_energy.png     energy split by node
    fig10_state_growth_N<n>.png  latency against round index (state growth)
    fig11_delta_heatmap.png   % change of every metric, with significance
    fig12_tradeoff.png        mouse slowdown against campaign length
"""

import argparse
import glob
import json
import os
import re
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from scipy import stats as sps
except ImportError:                                   # pragma: no cover
    sps = None

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

TAIL_S = 25.0          # seconds after the last request, so the final round is inside the window
MAX_GAP_S = 30.0       # power-sample gaps longer than this contribute no energy
DISPLAY = {"CAP", "WeightedLeastLoaded": "WLL","RandomNode","CLL"}
COLOR = {"CAP": "#1f77b4", "WLL": "#d62728", "RandomNode": "#ff7f0e", "CLL": "#2ca02c"}
CLASS_COLOR = {"mouse": "#4aa3df", "elephant": "#c0392b"}

RUN_RE = re.compile(r"^load_(?P<strategy>[A-Za-z]+?)_N(?P<n>\d+)_r(?P<rep>\d+)_rounds\.csv$")
DIR_RE = re.compile(r"^load_(?P<strategy>[A-Za-z]+)[-_](?P<mix>[0-9.]+%?)$")

# key -> (label, lower_is_better, unit)
METRICS = {
    "makespan":      ("Campaign makespan", True, "s"),
    "p50":           ("Round latency P50 (all)", True, "s"),
    "p90":           ("Round latency P90 (all)", True, "s"),
    "p99":           ("Round latency P99 (all)", True, "s"),
    "mouse_p50":     ("Mouse latency P50", True, "s"),
    "mouse_p90":     ("Mouse latency P90", True, "s"),
    "mouse_p99":     ("Mouse latency P99", True, "s"),
    "eleph_p50":     ("Elephant latency P50", True, "s"),
    "eleph_p99":     ("Elephant latency P99", True, "s"),
    "mouse_slow":    ("Mouse mean slowdown", True, "x"),
    "eleph_slow":    ("Elephant mean slowdown", True, "x"),
    "mouse_viol":    ("Mouse rounds over budget", True, "%"),
    "eleph_viol":    ("Elephant rounds over budget", True, "%"),
    "energy_kj":     ("Total energy", True, "kJ"),
    "j_per_token":   ("Energy per token", True, "J"),
    "throughput":    ("Cluster throughput", False, "tok/s"),
    "gpu_cv":        ("GPU load imbalance (CV)", True, ""),
}
HEADLINE = ["makespan", "p99", "eleph_p99", "mouse_p50", "mouse_p99",
            "mouse_slow", "mouse_viol", "j_per_token", "throughput"]
MOUSE_SIDE = ["mouse_p50", "mouse_p99", "mouse_slow", "mouse_viol"]
CAMPAIGN_SIDE = ["makespan", "p99", "eleph_p99", "j_per_token"]

def disp(s):
    return DISPLAY.get(s, s)


def mix_key(mouse_frac):
    """Nearest 5% of mice: the same workload can measure 0.486 at N=35 and 0.5 at N=50."""
    return round(mouse_frac * 20) / 20.0


def mix_label(k):
    m = int(round(k * 100))
    return f"{m}% mice / {100 - m}% elephants"


def hint_from_dir(path):
    m = DIR_RE.match(os.path.basename(os.path.dirname(path)))
    if not m:
        return None
    raw = m.group("mix")
    try:
        v = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    except ValueError:
        return None
    return v / 100 if v > 1 else v


def stars(p):
    if p != p:
        return ""
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"


def quant(v, q):
    v = np.asarray(v, float)
    v = v[~np.isnan(v)]
    return float(np.quantile(v, q)) if len(v) else float("nan")


def md_table(rows, header):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)

def energy_and_nodes(nodes_csv, lo, hi):
    """Per-node (energy J, mean GPU load %) over [lo, hi]; empty frame if unavailable."""
    if lo is None or not os.path.exists(nodes_csv):
        return pd.DataFrame(columns=["node", "energy_j", "gpu"])
    df = pd.read_csv(nodes_csv)
    for c in ("unix_ts", "gpu_load_perc", "active_power_mw"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df = df[(df["unix_ts"] >= lo) & (df["unix_ts"] <= hi)]
    df = df[(df["active_power_mw"] >= 0) & (df["gpu_load_perc"] >= 0)]
    out = []
    for node, g in df.groupby("node"):
        g = g.sort_values("unix_ts")
        dt = g["unix_ts"].diff()
        dt = dt.where((dt > 0) & (dt < MAX_GAP_S), 0.0)
        out.append({"node": node,
                    "energy_j": float((g["active_power_mw"] / 1000.0 * dt).sum()),
                    "gpu": float(g["gpu_load_perc"].mean())})
    return pd.DataFrame(out, columns=["node", "energy_j", "gpu"])


def read_one(path, args):
    df = pd.read_csv(path)
    for c in ("latency_s", "round", "t_send_unix", "prompt_tokens_est", "answer_tokens_est",
              "instrumentation", "timed_out"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["cls"] = df["agent_class"].astype(str).str.lower()
    df = df[df["cls"].isin(["mouse", "elephant"])]
    if df.empty:
        return None
    sess = df.groupby("session")["cls"].first()
    mouse_frac = float((sess == "mouse").mean())
    lo = df["t_send_unix"].min()
    hi = df["t_send_unix"].max() + TAIL_S
    timeouts = int(df["timed_out"].fillna(0).sum()) if "timed_out" in df else 0
    d = df[df["latency_s"] > 0].copy()
    if not args.include_instrumentation and "instrumentation" in d:
        d = d[d["instrumentation"].fillna(0) == 0]
    d = d[d["round"] >= args.skip_rounds]
    d["tokens"] = d["prompt_tokens_est"].fillna(0) + d["answer_tokens_est"].fillna(0)
    return d, mouse_frac, lo, hi, timeouts


def ingest(root, args):
    keep = {s.strip() for s in args.strategies.split(",")}
    found = defaultdict(list)                       # run key -> [paths]
    for p in sorted(glob.glob(os.path.join(root, "**", "*_rounds.csv"), recursive=True)):
        m = RUN_RE.match(os.path.basename(p))
        if not m or m.group("strategy") not in keep:
            continue
        found[(m.group("strategy"), int(m.group("n")), int(m.group("rep")), None)].append(p)
    print(f"[INFO] {sum(len(v) for v in found.values())} rounds files for {sorted(keep)}")

    runs, rounds, nodes = [], [], []
    dup_warned = False
    per_key = defaultdict(list)                     # (strategy, N, mixkey, rep) -> candidates
    for (strat, n, rep, _), paths in found.items():
        for p in paths:
            got = read_one(p, args)
            if got is None:
                print(f"  [SKIP] no usable rounds: {p}")
                continue
            d, mf, lo, hi, to = got
            hint = hint_from_dir(p)
            if hint is not None and abs(hint - mf) > 0.07 and abs((1 - hint) - mf) < 0.07:
                print(f"  [WARN] {os.path.basename(os.path.dirname(p))}: the number in the name "
                      f"({hint:.2f}) matches the ELEPHANT share of the data; the data win "
                      f"({mf:.0%} mice)")
            per_key[(strat, n, mix_key(mf), rep)].append((p, d, mf, lo, hi, to,
                                                           hint_from_dir(p) is not None))

    for key, cands in per_key.items():
        if len(cands) > 1:
            if not dup_warned:
                print("  [WARN] several files for the same (strategy, N, workload, repetition); "
                      "keeping the one inside a load_<Strategy>-<mix> directory")
                dup_warned = True
            cands.sort(key=lambda c: (not c[6], c[0]))
        p, d, mf, lo, hi, to, _ = cands[0]
        strat, n, mk, rep = key
        prefix = p[: -len("_rounds.csv")]
        makespan = float("nan")
        sj = prefix + "_summary.json"
        if os.path.exists(sj):
            try:
                makespan = json.load(open(sj)).get("aggregate_makespan_s") or float("nan")
            except (ValueError, OSError):
                pass
        if makespan != makespan:
            makespan = hi - lo - TAIL_S
        nd = energy_and_nodes(prefix + "_nodes.csv", lo, hi)
        energy = nd["energy_j"].sum() if len(nd) else float("nan")
        toks = d["tokens"].sum()

        mo = d[d["cls"] == "mouse"]["latency_s"].values
        el = d[d["cls"] == "elephant"]["latency_s"].values
        al = d["latency_s"].values
        sm, se = args.service["mouse"], args.service["elephant"]
        gcv = (nd["gpu"].std(ddof=0) / nd["gpu"].mean()) if len(nd) > 1 and nd["gpu"].mean() > 0 else float("nan")
        runs.append({
            "strategy": strat, "N": n, "mix": mk, "mix_label": mix_label(mk), "rep": rep,
            "makespan": makespan, "timeouts": to,
            "p50": quant(al, .5), "p90": quant(al, .9), "p99": quant(al, .99),
            "mouse_p50": quant(mo, .5), "mouse_p90": quant(mo, .9), "mouse_p99": quant(mo, .99),
            "eleph_p50": quant(el, .5), "eleph_p99": quant(el, .99),
            "mouse_slow": float(np.mean(mo) / sm) if len(mo) else float("nan"),
            "eleph_slow": float(np.mean(el) / se) if len(el) else float("nan"),
            "mouse_viol": float(np.mean(mo > args.slo_factor * sm) * 100) if len(mo) else float("nan"),
            "eleph_viol": float(np.mean(el > args.slo_factor * se) * 100) if len(el) else float("nan"),
            "energy_kj": energy / 1000.0,
            "j_per_token": energy / toks if toks else float("nan"),
            "throughput": toks / makespan if makespan else float("nan"),
            "gpu_cv": gcv, "rounds": len(d),
        })
        d = d.assign(strategy=strat, N=n, mix=mk, rep=rep)
        rounds.append(d[["strategy", "N", "mix", "rep", "session", "cls", "latency_s", "round", "tokens"]])
        if len(nd):
            nodes.append(nd.assign(strategy=strat, N=n, mix=mk, rep=rep))

    runs = pd.DataFrame(runs)
    rounds = pd.concat(rounds, ignore_index=True) if rounds else pd.DataFrame()
    nodes = pd.concat(nodes, ignore_index=True) if nodes else pd.DataFrame()
    return runs, rounds, nodes


# ------------------------------------------------------------------ statistics
def compare(a, b, seed=0):
    """CAP (a) against WLL (b), per-repetition values. Returns mean change and p-value."""
    a = np.asarray(a, float); a = a[~np.isnan(a)]
    b = np.asarray(b, float); b = b[~np.isnan(b)]
    out = {"n_a": len(a), "n_b": len(b), "mean_a": np.nan, "mean_b": np.nan,
           "pct": np.nan, "lo": np.nan, "hi": np.nan, "p": np.nan}
    if len(a) < 3 or len(b) < 3:
        return out
    out["mean_a"], out["mean_b"] = a.mean(), b.mean()
    if out["mean_b"] == 0:
        return out
    out["pct"] = (out["mean_a"] - out["mean_b"]) / out["mean_b"] * 100
    rng = np.random.default_rng(seed)
    ba = rng.choice(a, (2000, len(a))).mean(1)
    bb = rng.choice(b, (2000, len(b))).mean(1)
    pc = (ba - bb) / np.where(bb == 0, np.nan, bb) * 100
    out["lo"], out["hi"] = np.nanpercentile(pc, [2.5, 97.5])
    if sps is not None:
        try:
            out["p"] = float(sps.mannwhitneyu(a, b, alternative="two-sided").pvalue)
        except ValueError:
            out["p"] = 1.0
    else:                                                  
        obs = abs(a.mean() - b.mean()); pool = np.concatenate([a, b]); cnt = 0
        for _ in range(4000):
            rng.shuffle(pool)
            cnt += abs(pool[:len(a)].mean() - pool[len(a):].mean()) >= obs
        out["p"] = (cnt + 1) / 4001
    return out


def better(pct, metric):
    if pct != pct:
        return None
    return (pct < 0) if METRICS[metric][1] else (pct > 0)


def build_stats(runs, focal, base):
    rows = []
    for (mk, n), cell in runs.groupby(["mix", "N"]):
        for m in METRICS:
            a = cell[cell["strategy"] == focal][m].values
            b = cell[cell["strategy"] == base][m].values
            c = compare(a, b)
            rows.append({"mix": mk, "mix_label": mix_label(mk), "N": n, "metric": m, **c,
                         "better": better(c["pct"], m), "sig": stars(c["p"])})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ figures
def cells_of(runs):
    return sorted({(m, n) for m, n in zip(runs["mix"], runs["N"])})


def savefig(fig, outdir, name):
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, name), dpi=200)
    plt.close(fig)
    print(f"  wrote {name}")


def fig_ecdf(rounds, runs, outdir, args, focal, base):
    for n in sorted(rounds["N"].unique()):
        mixes = sorted(rounds[rounds["N"] == n]["mix"].unique())
        fig, axes = plt.subplots(len(mixes), 3, figsize=(13, 3.1 * len(mixes)), squeeze=False)
        for i, mk in enumerate(mixes):
            for j, (sel, ttl) in enumerate((("all", "All agents"), ("mouse", "Mice"), ("elephant", "Elephants"))):
                ax = axes[i][j]
                note = []
                for s in (focal, base):
                    d = rounds[(rounds["N"] == n) & (rounds["mix"] == mk) & (rounds["strategy"] == s)]
                    if sel != "all":
                        d = d[d["cls"] == sel]
                    v = np.sort(d["latency_s"].values)
                    if not len(v):
                        continue
                    ax.step(v, np.arange(1, len(v) + 1) / len(v), where="post", lw=1.7,
                            color=COLOR.get(disp(s)), label=disp(s))
                    note.append(f"{disp(s)}: P50 {np.quantile(v, .5):.1f}  P99 {np.quantile(v, .99):.1f}")
                if sel in ("mouse", "all"):
                    ax.axvline(args.service["mouse"], color="gray", ls=":", lw=0.9)
                if sel in ("elephant", "all"):
                    ax.axvline(args.service["elephant"], color="gray", ls=":", lw=0.9)
                ax.set_xscale("log"); ax.set_ylim(0, 1.01)
                ax.grid(alpha=0.3, ls="--", which="both")
                ax.text(0.98, 0.04, "\n".join(note), transform=ax.transAxes, ha="right",
                        va="bottom", fontsize=7, family="monospace",
                        bbox=dict(fc="white", ec="0.8", alpha=0.85))
                if i == 0:
                    ax.set_title(ttl)
                if j == 0:
                    ax.set_ylabel(f"{mix_label(mk)}\nECDF")
                if i == len(mixes) - 1:
                    ax.set_xlabel("Round latency (s, log scale)")
        axes[0][0].legend(frameon=False, fontsize=8, loc="upper left")
        fig.suptitle(f"Latency ECDF, N={n}  (dotted: isolated service time)", y=1.0)
        savefig(fig, outdir, f"fig01_ecdf_N{n}.png")


def grouped(ax, cell_list, runs, metrics_labels, focal, base, rotate=False):
    """Bars for focal vs base over (cell, metric) groups, mean with s.d. over repetitions."""
    groups = [(c, m) for c in cell_list for m in metrics_labels]
    x = np.arange(len(groups)); w = 0.38
    for k, s in enumerate((focal, base)):
        vals, errs = [], []
        for (mk, n), m in groups:
            v = runs[(runs["strategy"] == s) & (runs["mix"] == mk) & (runs["N"] == n)][m].values
            vals.append(np.nanmean(v) if len(v) else np.nan)
            errs.append(np.nanstd(v, ddof=1) if len(v) > 1 else 0)
        ax.bar(x + (k - 0.5) * w, vals, w, yerr=errs, capsize=2, color=COLOR.get(disp(s)),
               label=disp(s), error_kw={"lw": 0.8})
    return x, groups


def fig_percentiles(runs, outdir, focal, base):
    ns = sorted(runs["N"].unique()); mixes = sorted(runs["mix"].unique())
    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.2 * len(mixes), 3.2 * len(ns)), squeeze=False)
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            if not ((runs["N"] == n) & (runs["mix"] == mk)).any():
                ax.axis("off"); continue
            x, groups = grouped(ax, [(mk, n)], runs, ["p50", "p90", "p99"], focal, base)
            ax.set_xticks(x); ax.set_xticklabels(["P50", "P90", "P99"])
            ax.grid(alpha=0.3, ls="--", axis="y")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nlatency (s)")
    axes[0][0].legend(frameon=False)
    fig.suptitle("Round latency percentiles (all agents), mean over repetitions with s.d.", y=1.0)
    savefig(fig, outdir, "fig02_percentiles.png")


def fig_class_latency(runs, outdir, focal, base):
    ns = sorted(runs["N"].unique()); mixes = sorted(runs["mix"].unique())
    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.4 * len(mixes), 3.2 * len(ns)), squeeze=False)
    labels = {"mouse_p50": "Mouse\nP50", "mouse_p99": "Mouse\nP99",
              "eleph_p50": "Eleph.\nP50", "eleph_p99": "Eleph.\nP99"}
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            if not ((runs["N"] == n) & (runs["mix"] == mk)).any():
                ax.axis("off"); continue
            x, groups = grouped(ax, [(mk, n)], runs, list(labels), focal, base)
            ax.set_xticks(x); ax.set_xticklabels(list(labels.values()), fontsize=8)
            ax.grid(alpha=0.3, ls="--", axis="y")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nlatency (s)")
    axes[0][0].legend(frameon=False)
    fig.suptitle("Who pays for placement: mouse and elephant latency", y=1.0)
    savefig(fig, outdir, "fig03_class_latency.png")


def fig_makespan(runs, stats, outdir, focal, base):
    cl = cells_of(runs)
    fig, ax = plt.subplots(figsize=(max(7, 1.9 * len(cl)), 4.4))
    x = np.arange(len(cl)); w = 0.36
    rng = np.random.default_rng(1)
    top = 0
    for k, s in enumerate((focal, base)):
        for xi, (mk, n) in zip(x, cl):
            v = runs[(runs["strategy"] == s) & (runs["mix"] == mk) & (runs["N"] == n)]["makespan"].values
            if not len(v):
                continue
            ax.bar(xi + (k - .5) * w, v.mean(), w, color=COLOR.get(disp(s)), alpha=0.55,
                   label=disp(s) if xi == 0 else None)
            ax.errorbar(xi + (k - .5) * w, v.mean(), yerr=v.std(ddof=1) if len(v) > 1 else 0,
                        color="k", capsize=3, lw=0.9)
            ax.scatter(xi + (k - .5) * w + rng.uniform(-w / 4, w / 4, len(v)), v, s=9,
                       color=COLOR.get(disp(s)), edgecolor="k", lw=0.3, zorder=3)
            top = max(top, v.max())
    for xi, (mk, n) in zip(x, cl):
        r = stats[(stats["mix"] == mk) & (stats["N"] == n) & (stats["metric"] == "makespan")]
        if len(r) and r.iloc[0]["pct"] == r.iloc[0]["pct"]:
            r = r.iloc[0]
            ax.text(xi, top * 1.03, f"{r['pct']:+.1f}%\n{r['sig']}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(x); ax.set_xticklabels([f"{int(m*100)}% mice\nN={n}" for m, n in cl], fontsize=8)
    ax.set_ylabel("Campaign makespan (s)"); ax.set_ylim(0, top * 1.22)
    ax.set_title(f"Makespan; dots are repetitions; label = {disp(focal)} vs {disp(base)} (Mann-Whitney)")
    ax.legend(frameon=False); ax.grid(alpha=0.3, ls="--", axis="y")
    savefig(fig, outdir, "fig04_makespan.png")


def fig_contention(rounds, runs, outdir, args, focal, base):
    ns = sorted(rounds["N"].unique()); mixes = sorted(rounds["mix"].unique())
    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.4 * len(mixes), 3.4 * len(ns)), squeeze=False)
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            data, pos, cols, k = [], [], [], 0
            for cls in ("mouse", "elephant"):
                base_s = args.service[cls]
                for s in (focal, base):
                    d = rounds[(rounds["N"] == n) & (rounds["mix"] == mk) &
                               (rounds["strategy"] == s) & (rounds["cls"] == cls)]
                    if len(d):
                        data.append(d["latency_s"].values / base_s); pos.append(k); cols.append(COLOR.get(disp(s)))
                    k += 1
                k += 0.6
            if not data:
                ax.axis("off"); continue
            bp = ax.boxplot(data, positions=pos, widths=0.7, patch_artist=True, showfliers=False,
                            medianprops=dict(color="k"))
            for patch, c in zip(bp["boxes"], cols):
                patch.set_facecolor(c); patch.set_alpha(0.7)
            ax.set_yscale("log"); ax.axhline(1, color="gray", ls=":", lw=0.9)
            ax.set_xticks([0.5, 3.1]); ax.set_xticklabels(["Mice", "Elephants"])
            ax.grid(alpha=0.3, ls="--", axis="y", which="both")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nslowdown (x isolated)")
    h = [plt.Rectangle((0, 0), 1, 1, fc=COLOR.get(disp(s), "#7f7f7f"), alpha=.7) for s in (focal, base)]
    axes[0][0].legend(h, [disp(focal), disp(base)], frameon=False, fontsize=8)
    fig.suptitle("Contention: round latency divided by the isolated cost of its class (1 = no blocking)", y=1.0)
    savefig(fig, outdir, "fig05_contention.png")


def fig_slo(runs, outdir, args, focal, base):
    cl = cells_of(runs)
    fig, axes = plt.subplots(1, 2, figsize=(max(10, 2.1 * len(cl)), 4.2))
    for ax, m, ttl in ((axes[0], "mouse_viol", "Mice"), (axes[1], "eleph_viol", "Elephants")):
        x, groups = grouped(ax, cl, runs, [m], focal, base)
        ax.set_xticks(x); ax.set_xticklabels([f"{int(c[0]*100)}% mice\nN={c[1]}" for c, _ in groups], fontsize=8)
        ax.set_ylabel(f"% of rounds above {args.slo_factor:g}x isolated cost"); ax.set_title(ttl)
        ax.grid(alpha=0.3, ls="--", axis="y")
    axes[0].legend(frameon=False)
    fig.suptitle(f"Rounds that miss a {args.slo_factor:g}x slowdown budget", y=1.0)
    savefig(fig, outdir, "fig06_slo.png")


def fig_energy(runs, outdir, focal, base):
    cl = cells_of(runs)
    fig, axes = plt.subplots(1, 3, figsize=(max(14, 3.2 * len(cl)), 4.2))
    for ax, m, ttl, yl in ((axes[0], "j_per_token", "Energy per token", "J / token"),
                           (axes[1], "energy_kj", "Total energy", "kJ"),
                           (axes[2], "throughput", "Cluster throughput", "tokens / s")):
        x, groups = grouped(ax, cl, runs, [m], focal, base)
        ax.set_xticks(x); ax.set_xticklabels([f"{int(c[0]*100)}% mice\nN={c[1]}" for c, _ in groups], fontsize=8)
        ax.set_title(ttl); ax.set_ylabel(yl); ax.grid(alpha=0.3, ls="--", axis="y")
    axes[0].legend(frameon=False)
    savefig(fig, outdir, "fig07_energy.png")


def fig_nodes(nodes, runs, outdir, focal, base):
    if nodes.empty:
        return
    ns = sorted(nodes["N"].unique()); mixes = sorted(nodes["mix"].unique())
    names = sorted(nodes["node"].unique())
    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.6 * len(mixes), 3.3 * len(ns)), squeeze=False)
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            sub = nodes[(nodes["N"] == n) & (nodes["mix"] == mk)]
            if sub.empty:
                ax.axis("off"); continue
            x = np.arange(len(names)); w = 0.38
            for k, s in enumerate((focal, base)):
                g = sub[sub["strategy"] == s].groupby("node")["gpu"]
                mean = g.mean().reindex(names); sd = g.std().reindex(names).fillna(0)
                ax.bar(x + (k - .5) * w, mean.values, w, yerr=sd.values, capsize=2,
                       color=COLOR.get(disp(s)), label=disp(s), error_kw={"lw": .8})
            ax.set_xticks(x); ax.set_xticklabels([nm.replace("orin-", "o") for nm in names], fontsize=8)
            ax.set_ylim(0, 105); ax.grid(alpha=.3, ls="--", axis="y")
            cv = {s: runs[(runs["strategy"] == s) & (runs["N"] == n) & (runs["mix"] == mk)]["gpu_cv"].mean()
                  for s in (focal, base)}
            ax.text(0.02, 0.97, "imbalance CV  " + "  ".join(f"{disp(s)} {v:.2f}" for s, v in cv.items()),
                    transform=ax.transAxes, fontsize=7, va="top", family="monospace")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nmean GPU load (%)")
    axes[0][0].legend(frameon=False, loc="upper right", fontsize=8)
    fig.suptitle("Per-node GPU load (orin-20 is the slow node) and imbalance across nodes", y=1.0)
    savefig(fig, outdir, "fig08_node_balance.png")

    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.2 * len(mixes), 3.3 * len(ns)), squeeze=False)
    cmap = plt.get_cmap("tab10")
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            sub = nodes[(nodes["N"] == n) & (nodes["mix"] == mk)]
            if sub.empty:
                ax.axis("off"); continue
            bottom = np.zeros(2)
            for k, nm in enumerate(names):
                vals = [sub[(sub["strategy"] == s) & (sub["node"] == nm)]["energy_j"].mean() / 1000
                        for s in (focal, base)]
                vals = np.nan_to_num(vals)
                ax.bar([0, 1], vals, 0.6, bottom=bottom, color=cmap(k), label=nm)
                bottom += vals
            ax.set_xticks([0, 1]); ax.set_xticklabels([disp(focal), disp(base)])
            ax.grid(alpha=.3, ls="--", axis="y")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nenergy (kJ)")
    axes[0][-1].legend(frameon=False, fontsize=7, loc="upper left", bbox_to_anchor=(1, 1))
    fig.suptitle("Energy by node", y=1.0)
    savefig(fig, outdir, "fig09_node_energy.png")


def fig_growth(rounds, outdir, focal, base):
    for n in sorted(rounds["N"].unique()):
        mixes = sorted(rounds[rounds["N"] == n]["mix"].unique())
        fig, axes = plt.subplots(2, len(mixes), figsize=(4.4 * len(mixes), 6.2), squeeze=False)
        for j, mk in enumerate(mixes):
            for i, cls in enumerate(("mouse", "elephant")):
                ax = axes[i][j]
                for s in (focal, base):
                    d = rounds[(rounds["N"] == n) & (rounds["mix"] == mk) &
                               (rounds["strategy"] == s) & (rounds["cls"] == cls)]
                    if d.empty:
                        continue
                    g = d.groupby("round")["latency_s"]
                    med = g.median(); p90 = g.quantile(.9)
                    ax.plot(med.index, med.values, "-o", ms=3, color=COLOR.get(disp(s)), label=f"{disp(s)} median")
                    ax.plot(p90.index, p90.values, "--", color=COLOR.get(disp(s)), alpha=.6, label=f"{disp(s)} P90")
                ax.grid(alpha=.3, ls="--")
                if i == 0:
                    ax.set_title(mix_label(mk), fontsize=10)
                ax.set_ylabel(f"{cls} latency (s)")
                if i == 1:
                    ax.set_xlabel("round index (conversation length)")
        axes[0][0].legend(frameon=False, fontsize=7)
        fig.suptitle(f"Latency as the conversation grows, N={n}", y=1.0)
        savefig(fig, outdir, f"fig10_state_growth_N{n}.png")


def fig_delta(stats, outdir, focal, base):
    cols = sorted({(m, n) for m, n in zip(stats["mix"], stats["N"])})
    mets = [m for m in HEADLINE if m in METRICS]
    ben = np.full((len(mets), len(cols)), np.nan)
    txt = [[""] * len(cols) for _ in mets]
    for j, (mk, n) in enumerate(cols):
        for i, m in enumerate(mets):
            r = stats[(stats["mix"] == mk) & (stats["N"] == n) & (stats["metric"] == m)]
            if r.empty or r.iloc[0]["pct"] != r.iloc[0]["pct"]:
                continue
            r = r.iloc[0]
            ben[i, j] = (-r["pct"] if METRICS[m][1] else r["pct"])
            txt[i][j] = f"{r['pct']:+.0f}%\n{r['sig']}"
    fig, ax = plt.subplots(figsize=(max(7, 1.5 * len(cols) + 3), 0.62 * len(mets) + 2.2))
    im = ax.imshow(np.clip(ben, -60, 60), cmap="RdYlGn", vmin=-60, vmax=60, aspect="auto")
    for i in range(len(mets)):
        for j in range(len(cols)):
            ax.text(j, i, txt[i][j], ha="center", va="center", fontsize=7)
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels([f"{int(m*100)}% mice\nN={n}" for m, n in cols], fontsize=8)
    ax.set_yticks(range(len(mets))); ax.set_yticklabels([METRICS[m][0] for m in mets], fontsize=8)
    cb = fig.colorbar(im, ax=ax); cb.set_label(f"green: {disp(focal)} better   red: {disp(base)} better")
    ax.set_title(f"{disp(focal)} versus {disp(base)}: % change (stars: Mann-Whitney p; ns: not significant)")
    savefig(fig, outdir, "fig11_delta_heatmap.png")


def fig_tradeoff(runs, outdir, focal, base):
    ns = sorted(runs["N"].unique()); mixes = sorted(runs["mix"].unique())
    fig, axes = plt.subplots(len(ns), len(mixes), figsize=(4.2 * len(mixes), 3.4 * len(ns)), squeeze=False)
    for i, n in enumerate(ns):
        for j, mk in enumerate(mixes):
            ax = axes[i][j]
            for s in (focal, base):
                d = runs[(runs["strategy"] == s) & (runs["N"] == n) & (runs["mix"] == mk)]
                if d.empty:
                    continue
                ax.scatter(d["makespan"], d["mouse_slow"], s=14, alpha=.6, color=COLOR.get(disp(s)))
                ax.scatter(d["makespan"].mean(), d["mouse_slow"].mean(), marker="X", s=90,
                           color=COLOR.get(disp(s)), edgecolor="k", label=disp(s))
            ax.grid(alpha=.3, ls="--")
            if i == 0:
                ax.set_title(mix_label(mk), fontsize=10)
            if j == 0:
                ax.set_ylabel(f"N={n}\nmouse slowdown")
            if i == len(ns) - 1:
                ax.set_xlabel("makespan (s)")
    axes[0][0].legend(frameon=False, fontsize=8)
    fig.suptitle("Trade-off: down is better for mice, left is a shorter campaign (X = mean)", y=1.0)
    savefig(fig, outdir, "fig12_tradeoff.png")


# ------------------------------------------------------------------ report
def verdict_lines(stats, focal, base):
    F, B = disp(focal), disp(base)
    lines = []
    for mk in sorted(stats["mix"].unique()):
        sub = stats[(stats["mix"] == mk) & (stats["sig"].isin(["*", "**", "***"]))]
        def tally(metrics, who):
            s = sub[sub["metric"].isin(metrics)]
            return int(((s["better"] == (who == "f"))).sum())
        mf, mb = tally(MOUSE_SIDE, "f"), tally(MOUSE_SIDE, "b")
        cf, cb = tally(CAMPAIGN_SIDE, "f"), tally(CAMPAIGN_SIDE, "b")
        txt = (f"**{mix_label(mk)}**: significant wins on mouse-side metrics {F} {mf} / {B} {mb}; "
               f"on campaign-side metrics {F} {cf} / {B} {cb}. ")
        if mf > mb and cb > cf:
            txt += f"A trade-off: {F} protects short agents, {B} finishes the campaign sooner."
        elif mf > mb and cf >= cb:
            txt += f"{F} is better for short agents without a campaign-side penalty."
        elif mb > mf and cb > cf:
            txt += f"{B} is better on both sides."
        elif cf > cb and mb >= mf:
            txt += f"{F} is better on the campaign side."
        elif mf == mb == cf == cb == 0:
            txt += "No significant difference: the two policies are equivalent here."
        else:
            txt += "Mixed; read the per-metric table."
        lines.append("- " + txt)
    return lines


def write_report(runs, stats, outdir, args, focal, base, figs):
    F, B = disp(focal), disp(base)
    L = [f"# {F} versus {B}: placement comparison", "",
         f"Generated from `{args.campaign}`. Composition of each run is measured from its "
         f"`agent_class` column. Statistics are per repetition (Mann-Whitney U, two-sided; "
         f"bootstrap 95% interval on the % change). Many comparisons are made, so treat a lone "
         f"`*` as suggestive and `**`/`***` as solid.", ""]
    inv = runs.groupby(["mix_label", "N", "strategy"]).agg(
        reps=("rep", "count"), timeouts=("timeouts", "sum")).reset_index()
    L += ["## 1. Data inventory", "",
          md_table([[r.mix_label, r.N, disp(r.strategy), r.reps, int(r.timeouts)] for r in inv.itertuples()],
                   ["workload", "N", "strategy", "repetitions", "timed-out rounds"]), ""]
    if inv["timeouts"].sum():
        L += [f"> **Warning:** {int(inv['timeouts'].sum())} rounds timed out. Check the affected runs "
              f"before quoting numbers.", ""]
    L += ["## 2. Verdict by workload (auto-generated; verify against the tables)", ""]
    L += verdict_lines(stats, focal, base) + [""]

    L += ["## 3. Headline metrics, every workload and load", ""]
    rows = []
    for (mk, n), cell in stats.groupby(["mix", "N"]):
        for m in HEADLINE:
            r = cell[cell["metric"] == m]
            if r.empty or r.iloc[0]["pct"] != r.iloc[0]["pct"]:
                continue
            r = r.iloc[0]
            w = {None: "", True: F, False: B}[r["better"]] if r["sig"] != "ns" else "tie"
            rows.append([mix_label(mk), n, METRICS[m][0], f"{r['mean_a']:.3g}", f"{r['mean_b']:.3g}",
                         f"{r['pct']:+.1f}% [{r['lo']:+.0f}, {r['hi']:+.0f}]", f"{r['p']:.3f}", r["sig"], w])
    L += [md_table(rows, ["workload", "N", "metric", F, B, f"change [95% CI]", "p", "sig", "better"]), ""]

    L += ["## 4. Per-cell means ± s.d. over repetitions", ""]
    cm = []
    for (mk, n, s), g in runs.groupby(["mix", "N", "strategy"]):
        def ms(c):
            v = g[c].dropna()
            return f"{v.mean():.1f} ± {v.std(ddof=1):.1f}" if len(v) > 1 else (f"{v.mean():.1f}" if len(v) else "n/a")
        cm.append([mix_label(mk), n, disp(s), ms("makespan"), ms("p50"), ms("p99"), ms("mouse_p50"),
                   ms("mouse_p99"), ms("eleph_p99"), f"{g['mouse_slow'].mean():.2f}",
                   f"{g['j_per_token'].mean():.2f}", f"{g['throughput'].mean():.1f}"])
    L += [md_table(cm, ["workload", "N", "strategy", "makespan (s)", "P50", "P99", "mouse P50",
                        "mouse P99", "eleph P99", "mouse slow", "J/token", "tok/s"]), ""]

    L += ["## 5. How to read the figures", ""]
    for name, what in figs:
        L.append(f"- `{name}`: {what}")
    L += ["", "## 6. Caveats", "",
          "- Slowdown uses the isolated costs given by `--service` "
          f"(mouse {args.service['mouse']} s, elephant {args.service['elephant']} s); change them if recalibrated.",
          "- Instrumentation rounds (trend/recall probes) are excluded unless `--include-instrumentation` is set.",
          f"- Rounds before index {args.skip_rounds} are dropped (`--skip-rounds`).",
          "- Energy covers the window from the first request to the last answer; nodes with an unreadable "
          "meter are excluded from both strategies.",
          "- 'Better' means a lower value, except throughput. A policy can win the campaign-side metrics "
          "and lose the mouse-side ones; section 2 states which."]
    open(os.path.join(outdir, "REPORT.md"), "w").write("\n".join(L) + "\n")
    print("  wrote REPORT.md")


FIGS = [
    ("fig01_ecdf_N<n>.png", "latency ECDF per workload (rows) for all agents, mice and elephants; "
                            "a curve further left and steeper is better; the right tail is the P99"),
    ("fig02_percentiles.png", "P50/P90/P99 of all rounds per workload and load"),
    ("fig03_class_latency.png", "mouse and elephant P50/P99: shows who pays for the placement"),
    ("fig04_makespan.png", "campaign length with every repetition; the label is the % change and its significance"),
    ("fig05_contention.png", "latency divided by the isolated cost of the class (1 = no head-of-line blocking)"),
    ("fig06_slo.png", "share of rounds that exceed the slowdown budget"),
    ("fig07_energy.png", "energy per token, total energy and cluster throughput"),
    ("fig08_node_balance.png", "per-node GPU load; the CV annotation measures imbalance across nodes"),
    ("fig09_node_energy.png", "energy split by node"),
    ("fig10_state_growth_N<n>.png", "latency against round index: how each policy copes as the conversation grows"),
    ("fig11_delta_heatmap.png", "one-page summary: green = first strategy better, red = second better"),
    ("fig12_tradeoff.png", "mouse slowdown against makespan per repetition"),
]


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", default="results/campaign")
    ap.add_argument("--out", default="results/compare")
    ap.add_argument("--strategies", default="Intelligent,WeightedLeastLoaded",
                    help="focal strategy first, baseline second")
    ap.add_argument("--service", default="mouse=1.06,elephant=5.77",
                    help="isolated round time per class, for slowdown")
    ap.add_argument("--slo-factor", type=float, default=5.0,
                    help="a round 'misses budget' when latency exceeds this multiple of its isolated cost")
    ap.add_argument("--skip-rounds", type=int, default=0)
    ap.add_argument("--include-instrumentation", action="store_true")
    args = ap.parse_args()

    args.service = {k.strip(): float(v) for k, v in
                    (kv.split("=") for kv in args.service.split(",") if "=" in kv)}
    names = [s.strip() for s in args.strategies.split(",")]
    if len(names) != 2:
        raise SystemExit("[ERROR] --strategies needs exactly two names: focal,baseline")
    focal, base = names
    if not os.path.isdir(args.campaign):
        raise SystemExit(f"[ERROR] not a directory: {args.campaign}")
    os.makedirs(args.out, exist_ok=True)

    runs, rounds, nodes = ingest(args.campaign, args)
    if runs.empty:
        raise SystemExit("[ERROR] no runs parsed; expected load_<Strategy>_N<n>_r<rep>_rounds.csv")
    have = set(runs["strategy"].unique())
    if not {focal, base} <= have:
        raise SystemExit(f"[ERROR] need both strategies, found {sorted(have)}")
    print("[INFO] runs: " + ", ".join(f"{disp(s)} x{c}" for s, c in runs["strategy"].value_counts().items()))
    for (mk, n), c in runs.groupby(["mix", "N"]):
        counts = ", ".join(f"{disp(s)} {k}" for s, k in c["strategy"].value_counts().items())
        print(f"       {mix_label(mk)}, N={n}: {counts}")

    stats = build_stats(runs, focal, base)
    runs.to_csv(os.path.join(args.out, "runs.csv"), index=False)
    stats.to_csv(os.path.join(args.out, "stats.csv"), index=False)
    (runs.groupby(["mix_label", "N", "strategy"])[[m for m in METRICS]].mean()
         .reset_index().to_csv(os.path.join(args.out, "cells.csv"), index=False))

    print("[INFO] figures")
    fig_ecdf(rounds, runs, args.out, args, focal, base)
    fig_percentiles(runs, args.out, focal, base)
    fig_class_latency(runs, args.out, focal, base)
    fig_makespan(runs, stats, args.out, focal, base)
    fig_contention(rounds, runs, args.out, args, focal, base)
    fig_slo(runs, args.out, args, focal, base)
    fig_energy(runs, args.out, focal, base)
    fig_nodes(nodes, runs, args.out, focal, base)
    fig_growth(rounds, args.out, focal, base)
    fig_delta(stats, args.out, focal, base)
    fig_tradeoff(runs, args.out, focal, base)
    write_report(runs, stats, args.out, args, focal, base, FIGS)

    print("\n=== verdict ===")
    for l in verdict_lines(stats, focal, base):
        print(l.replace("**", ""))
    print(f"\n[DONE] open {os.path.join(args.out, 'REPORT.md')}")


if __name__ == "__main__":
    main()