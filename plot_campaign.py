#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
    python3 plot_campaign.py
"""

import argparse
import glob
import json
import os
import re

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# window: seconds after the last prompt, to let the final round's inference finish
TAIL_S = 25.0
# ignore inter-sample gaps longer than this (s) when integrating power
MAX_GAP_S = 30.0

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
    "font.size": 11,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 9,
    "figure.titlesize": 14,
    "figure.dpi": 300,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "axes.grid": True,
    "grid.alpha": 0.3,
    "grid.linestyle": "--",
})

# fixed strategy order and colours, so every figure is consistent
STRAT_ORDER = ["CAP", "RandomNode", "Random", "RoundRobin"]
STRAT_PALETTE = {
    "CAP": "#1f77b4",
    "RandomNode": "#ff7f0e",
    "Random": "#2ca02c",
    "RoundRobin": "#d62728",
}
CLASS_PALETTE = {"Elephant": "crimson", "Mouse": "dodgerblue"}
# 1. PARSING & METRIC EXTRACTION
def parse_run_topology(instances_path, fids_path):
    if not (os.path.exists(instances_path) and os.path.exists(fids_path)):
        return {"colocation_ratio": np.nan, "avg_hops_per_wf": np.nan}

    node_instances = []
    current_node = None
    with open(instances_path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("["):
                match = re.match(r"\[(?P<kind>F|R)\]\s+(?P<fid>[a-f0-9-]+)", line)
                if match and current_node:
                    node_instances.append({
                        "node": current_node,
                        "kind": "Function" if match.group("kind") == "F" else "Resource",
                        "fid": match.group("fid"),
                    })
            elif line:
                current_node = line

    df_inst = pd.DataFrame(node_instances)
    if df_inst.empty:
        return {"colocation_ratio": np.nan, "avg_hops_per_wf": np.nan}

    fid_pattern = re.compile(
        r"workflow\s+(?P<wf_id>[a-f0-9-]+)\s+(?P<comp_type>function|resource)\s+"
        r"(?P<comp_name>[\w-]+)\s+started\s+with\s+fid\s+(?P<fid>[a-f0-9-]+)"
    )
    fid_mappings = []
    with open(fids_path, "r") as f:
        for line in f:
            m = fid_pattern.search(line)
            if m:
                fid_mappings.append(m.groupdict())

    df_fids = pd.DataFrame(fid_mappings)
    if df_fids.empty:
        return {"colocation_ratio": np.nan, "avg_hops_per_wf": np.nan}

    df_topology = pd.merge(df_fids, df_inst, on="fid", how="left")

    colocated, total_hops = 0, 0
    grouped = df_topology.groupby("wf_id")
    for _, group in grouped:
        total_hops += max(0, group["node"].nunique() - 1)
        compute_nodes = group[group["comp_name"] != "http-ingress"]["node"].nunique()
        if compute_nodes == 1:
            colocated += 1

    n = len(grouped)
    return {
        "colocation_ratio": (colocated / n * 100) if n else 0.0,
        "avg_hops_per_wf": (total_hops / n) if n else 0.0,
    }


def windowed_energy(df_nodes, rounds_csv):
    """Return (df_nodes with per-sample energy_j over the run window, window_s).

    The window is [first prompt, last prompt + TAIL_S] from the driver's own
    t_send_unix. Samples outside it are dropped; gaps > MAX_GAP_S contribute no
    energy. This is what keeps a 2x-faster policy from appearing to use more
    energy than it does.
    """
    df_nodes = df_nodes.copy()
    span = np.nan
    if os.path.exists(rounds_csv):
        ts = pd.to_numeric(pd.read_csv(rounds_csv).get("t_send_unix"), errors="coerce").dropna()
        if len(ts):
            lo, hi = ts.min(), ts.max() + TAIL_S
            df_nodes = df_nodes[(df_nodes["unix_ts"] >= lo) & (df_nodes["unix_ts"] <= hi)].copy()
            span = hi - lo

    df_nodes["active_power_mw"] = df_nodes["active_power_mw"].clip(lower=0)
    df_nodes["dt"] = df_nodes.groupby("node")["unix_ts"].diff()
    df_nodes["dt"] = df_nodes["dt"].where((df_nodes["dt"] > 0) & (df_nodes["dt"] < MAX_GAP_S), 0.0)
    df_nodes["energy_j"] = (df_nodes["active_power_mw"] / 1000.0) * df_nodes["dt"]
    return df_nodes, span


def ingest_campaign_data(results_dir):
    load_pat = re.compile(r"load_(?P<strategy>[\w-]+)_N(?P<n>\d+)_r(?P<rep>\d+)_nodes\.csv")
    calib_pat = re.compile(r"calib_(?P<workload>elephant|mouse)_r(?P<rep>\d+)_nodes\.csv")
    run_records, node_records, round_records = [], [], []

    files = sorted(glob.glob(os.path.join(results_dir, "*_nodes.csv")))
    print(f"[INFO] {len(files)} telemetry node files in '{results_dir}'")

    for nodes_csv in files:
        name = os.path.basename(nodes_csv)
        lm, cm = load_pat.match(name), calib_pat.match(name)
        if lm:
            strategy = lm.group("strategy")
            n_workflows = int(lm.group("n"))
            repetition = int(lm.group("rep"))
            run_type, workload_cat = "load", "Mixed"
        elif cm:
            wl = cm.group("workload").capitalize()
            strategy = f"Calib ({wl})"
            n_workflows = 1
            repetition = int(cm.group("rep"))
            run_type, workload_cat = "calibration", wl
        else:
            continue

        prefix = nodes_csv.replace("_nodes.csv", "")
        run_id = os.path.basename(prefix)
        rounds_csv = f"{prefix}_rounds.csv"
        instances_txt = f"{prefix}_instances.txt"
        fids_txt = f"{prefix}_fids.txt"

        if not os.path.exists(rounds_csv):
            print(f"  [SKIP] missing rounds CSV: {run_id}")
            continue

        # --- node telemetry, integrated over the run window ---
        df_nodes_raw = pd.read_csv(nodes_csv)
        df_nodes, window_s = windowed_energy(df_nodes_raw, rounds_csv)
        if df_nodes.empty:
            print(f"  [SKIP] no samples in window: {run_id}")
            continue

        total_energy_j = df_nodes["energy_j"].sum()
        has_gpu = "gpu_load_perc" in df_nodes.columns
        avg_gpu_load = df_nodes["gpu_load_perc"].mean() if has_gpu else np.nan

        agg = {"node_energy_j": ("energy_j", "sum"),
               "avg_power_mw": ("active_power_mw", "mean")}
        if has_gpu:
            agg["avg_gpu_load"] = ("gpu_load_perc", "mean")
        per_node = df_nodes.groupby("node").agg(**agg).reset_index()
        if not has_gpu:
            per_node["avg_gpu_load"] = np.nan
        per_node["Strategy"] = strategy
        per_node["N_Workflows"] = n_workflows
        per_node["Run_Type"] = run_type
        per_node["Workload_Cat"] = workload_cat
        per_node["Run_ID"] = run_id
        node_records.append(per_node)

        # --- rounds & latency ---
        df_rounds = pd.read_csv(rounds_csv)
        df_rounds["Strategy"] = strategy
        df_rounds["N_Workflows"] = n_workflows
        df_rounds["Run_Type"] = run_type
        df_rounds["Run_ID"] = run_id
        df_rounds["total_tokens"] = (
            pd.to_numeric(df_rounds.get("prompt_tokens_est"), errors="coerce").fillna(0)
            + pd.to_numeric(df_rounds.get("answer_tokens_est"), errors="coerce").fillna(0)
        )

        # true class from the driver, not a per-run token median (which mislabels
        # an elephant's small early rounds as mice)
        if run_type == "calibration":
            df_rounds["Workload_Class"] = workload_cat
        elif "agent_class" in df_rounds.columns:
            df_rounds["Workload_Class"] = (
                df_rounds["agent_class"].astype(str).str.capitalize()
                .replace({"Uniform": "Mouse"})
            )
        else:
            med = df_rounds["total_tokens"].median()
            df_rounds["Workload_Class"] = np.where(
                df_rounds["total_tokens"] >= med, "Elephant", "Mouse")
        round_records.append(df_rounds)

        total_tokens = df_rounds["total_tokens"].sum()
        lat = pd.to_numeric(df_rounds.get("latency_s"), errors="coerce")
        valid = df_rounds[lat > 0]
        tps = ((valid["total_tokens"] / pd.to_numeric(valid["latency_s"])).mean()
               if not valid.empty else 0.0)
        jpt = (total_energy_j / total_tokens) if total_tokens > 0 else np.nan
        timeouts = int(pd.to_numeric(df_rounds.get("timed_out"), errors="coerce").fillna(0).sum())

        topo = parse_run_topology(instances_txt, fids_txt)

        # --- makespan, from the driver's own summary ---
        makespan, spread = np.nan, np.nan
        summary_json = f"{prefix}_summary.json"
        if os.path.exists(summary_json):
            try:
                sj = json.load(open(summary_json))
                makespan = sj.get("aggregate_makespan_s") or np.nan
                sess = [x.get("makespan_s") for x in sj.get("sessions", []) if x.get("makespan_s")]
                if sess:
                    spread = max(sess) / min(sess)
            except (ValueError, OSError):
                pass

        run_records.append({
            "Run_ID": run_id, "Strategy": strategy, "N_Workflows": n_workflows,
            "Repetition": repetition, "Run_Type": run_type, "Workload_Cat": workload_cat,
            "Total_Energy_J": total_energy_j, "Window_s": window_s,
            "Makespan_s": makespan, "Session_Spread": spread,
            "Avg_GPU_Load_%": avg_gpu_load, "Tokens_Per_Sec": tps,
            "Joules_Per_Token": jpt, "Timeouts": timeouts,
            "Colocation_Ratio_%": topo["colocation_ratio"],
            "Avg_Hops_Per_WF": topo["avg_hops_per_wf"],
        })

    return (
        pd.DataFrame(run_records),
        pd.concat(node_records, ignore_index=True) if node_records else pd.DataFrame(),
        pd.concat(round_records, ignore_index=True) if round_records else pd.DataFrame(),
    )


def order(df, col="Strategy"):
    present = [s for s in STRAT_ORDER if s in df[col].unique()]
    extra = [s for s in df[col].unique() if s not in STRAT_ORDER]
    return present + extra


# ==============================================================================
# 2. FIGURES
# ==============================================================================
def generate_all_plots(df_runs, df_nodes, df_rounds, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    load_runs = df_runs[df_runs["Run_Type"] == "load"].copy()
    load_nodes = df_nodes[df_nodes["Run_Type"] == "load"].copy()
    ho = order(load_runs)

    # -- print the numbers the figures are built from, so they can be checked --
    print("\n=== energy / efficiency summary (mean over reps) ===")
    chk = (load_runs.groupby(["Strategy", "N_Workflows"])
           .agg(J_per_token=("Joules_Per_Token", "mean"),
                total_kJ=("Total_Energy_J", lambda x: x.mean() / 1000),
                window_s=("Window_s", "mean"),
                tok_s=("Tokens_Per_Sec", "mean"))
           .reset_index().sort_values(["N_Workflows", "Strategy"]))
    print(chk.to_string(index=False,
          formatters={"J_per_token": "{:.2f}".format, "total_kJ": "{:.1f}".format,
                      "window_s": "{:.0f}".format, "tok_s": "{:.1f}".format}))
    print()

    # ------- FIGURE 1: efficiency first, total second -------
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    sns.lineplot(data=load_runs, x="N_Workflows", y="Joules_Per_Token", hue="Strategy",
                 hue_order=ho, palette=STRAT_PALETTE, style="Strategy", style_order=ho,
                 markers=True, ax=ax1, linewidth=2, markersize=8, errorbar="sd")
    ax1.set_title("(a) Energy Cost per Token")
    ax1.set_ylabel("Joules / Token")
    ax1.set_xlabel("Concurrent Agents N")
    sns.lineplot(data=load_runs, x="N_Workflows", y="Total_Energy_J", hue="Strategy",
                 hue_order=ho, palette=STRAT_PALETTE, style="Strategy", style_order=ho,
                 markers=True, ax=ax2, linewidth=2, markersize=8, errorbar="sd")
    ax2.set_title("(b) Cluster Total Energy")
    ax2.set_ylabel("Total Energy (Joules)")
    ax2.set_xlabel("Concurrent Agents N")
    plt.tight_layout()
    for ext in ("pdf", "png"):
        plt.savefig(os.path.join(output_dir, f"fig1_energy_and_efficiency.{ext}"))
    plt.close()

    # ------- FIGURE 2: throughput; timeouts as an annotation if all zero -------
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    sns.lineplot(data=load_runs, x="N_Workflows", y="Tokens_Per_Sec", hue="Strategy",
                 hue_order=ho, palette=STRAT_PALETTE, style="Strategy", style_order=ho,
                 markers=True, ax=ax1, linewidth=2, markersize=8, errorbar="sd")
    ax1.set_title("(a) Inference Throughput")
    ax1.set_ylabel("Tokens / Second")
    ax1.set_xlabel("Concurrent Agents N")

    total_to = load_runs["Timeouts"].sum()
    if total_to == 0:
        ax2.axis("off")
        ax2.text(0.5, 0.5, "Zero workflow timeouts\nacross all strategies and loads",
                 ha="center", va="center", fontsize=12, transform=ax2.transAxes)
        ax2.set_title("(b) Cumulative Workflow Timeouts")
    else:
        td = load_runs.groupby(["Strategy", "N_Workflows"])["Timeouts"].sum().reset_index()
        sns.barplot(data=td, x="N_Workflows", y="Timeouts", hue="Strategy",
                    hue_order=ho, palette=STRAT_PALETTE, ax=ax2)
        ax2.set_title("(b) Cumulative Workflow Timeouts")
        ax2.set_ylabel("Total Timed-Out Requests")
    plt.tight_layout()
    for ext in ("pdf", "png"):
        plt.savefig(os.path.join(output_dir, f"fig2_throughput_and_timeouts.{ext}"))
    plt.close()

    # ------- FIGURE 3: topology -------
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    sns.lineplot(data=load_runs, x="N_Workflows", y="Colocation_Ratio_%", hue="Strategy",
                 hue_order=ho, palette=STRAT_PALETTE, style="Strategy", style_order=ho,
                 markers=True, ax=ax1, linewidth=2, markersize=8, errorbar="sd")
    ax1.set_title("(a) Compute Component Colocation Ratio")
    ax1.set_ylabel("Single-Node Colocation (%)")
    ax1.set_xlabel("Concurrent Agents N")
    sns.lineplot(data=load_runs, x="N_Workflows", y="Avg_Hops_Per_WF", hue="Strategy",
                 hue_order=ho, palette=STRAT_PALETTE, style="Strategy", style_order=ho,
                 markers=True, ax=ax2, linewidth=2, markersize=8, errorbar="sd")
    ax2.set_title("(b) Average Inter-Node Network Hops")
    ax2.set_ylabel("Average Hops / Workflow")
    ax2.set_xlabel("Concurrent Agents N")
    plt.tight_layout()
    for ext in ("pdf", "png"):
        plt.savefig(os.path.join(output_dir, f"fig3_topology_placement_metrics.{ext}"))
    plt.close()

    # ------- FIGURE 4: per-node GPU load and energy heatmap -------
    if not load_nodes.empty:
        want = [5, 20, 35]
        avail = [n for n in want if n in load_nodes["N_Workflows"].unique()] \
            or sorted(load_nodes["N_Workflows"].unique())
        fig, axes = plt.subplots(len(avail), 2, figsize=(15, 4.5 * len(avail)))
        if len(avail) == 1:
            axes = np.array([axes])
        for idx, nval in enumerate(avail):
            ax_bar, ax_heat = axes[idx, 0], axes[idx, 1]
            nd = load_nodes[load_nodes["N_Workflows"] == nval]
            sns.barplot(data=nd, x="node", y="avg_gpu_load", hue="Strategy",
                        hue_order=order(nd), palette=STRAT_PALETTE, ax=ax_bar, errorbar="sd")
            ax_bar.set_title(f"(a) Node GPU Load Distribution at N={nval}")
            ax_bar.set_xlabel("Physical Node")
            ax_bar.set_ylabel("Average GPU Load (%)")
            ax_bar.set_ylim(0, 105)
            plt.setp(ax_bar.get_xticklabels(), rotation=30, ha="right")
            ax_bar.legend(title="Strategy", bbox_to_anchor=(1.02, 1), loc="upper left")
            pivot = nd.pivot_table(index="node", columns="Strategy",
                                   values="node_energy_j", aggfunc="mean").fillna(0)
            pivot = pivot[[c for c in STRAT_ORDER if c in pivot.columns]]
            sns.heatmap(pivot, annot=True, fmt=".0f", cmap="YlGnBu", ax=ax_heat,
                        cbar_kws={"label": "Energy (Joules)"})
            ax_heat.set_title(f"(b) Per-Node Energy Heatmap at N={nval}")
            ax_heat.set_xlabel("Placement Strategy")
            ax_heat.set_ylabel("Physical Node")
        plt.tight_layout()
        for ext in ("pdf", "png"):
            plt.savefig(os.path.join(output_dir, f"fig4_node_level_balancing.{ext}"))
        plt.close()

    # ------- FIGURE 5: latency CDF -------
    if not df_rounds.empty:
        lat = pd.to_numeric(df_rounds["latency_s"], errors="coerce")
        v = df_rounds[lat > 0].copy()
        vl = v[v["Run_Type"] == "load"]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
        sns.ecdfplot(data=vl, x="latency_s", hue="Strategy", hue_order=order(vl),
                     palette=STRAT_PALETTE, linewidth=2, ax=ax1)
        ax1.set_title("(a) End-to-End Latency CDF by Strategy")
        ax1.set_xlabel("Latency (seconds)")
        ax1.set_ylabel("ECDF")
        sns.ecdfplot(data=v, x="latency_s", hue="Workload_Class", linewidth=2, ax=ax2,
                     palette=CLASS_PALETTE)
        ax2.set_title("(b) Latency CDF: Elephant vs. Mouse")
        ax2.set_xlabel("Latency (seconds)")
        ax2.set_ylabel("ECDF")
        plt.tight_layout()
        for ext in ("pdf", "png"):
            plt.savefig(os.path.join(output_dir, f"fig5_latency_ecdf.{ext}"))
        plt.close()

    # ------- FIGURE 6: class profiling -------
    if not df_rounds.empty:
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(18, 5))
        lr = df_rounds[df_rounds["Run_Type"] == "load"]
        sns.boxplot(data=lr, x="Strategy", y="total_tokens", hue="Workload_Class",
                    order=order(lr), palette=CLASS_PALETTE, ax=ax1)
        ax1.set_title("(a) Token Distribution by Strategy & Class")
        ax1.set_xlabel("Placement Strategy")
        ax1.set_ylabel("Total Tokens (Prompt + Answer)")
        plt.setp(ax1.get_xticklabels(), rotation=30, ha="right")
        sns.barplot(data=df_nodes, x="node", y="avg_power_mw", hue="Workload_Cat",
                    palette="Set1", ax=ax2, errorbar="sd")
        ax2.set_title("(b) Average Node Power Draw (mW)")
        ax2.set_xlabel("Physical Node")
        ax2.set_ylabel("Power (mW)")
        plt.setp(ax2.get_xticklabels(), rotation=30, ha="right")
        lat = pd.to_numeric(df_rounds["latency_s"], errors="coerce")
        sns.scatterplot(data=df_rounds[lat > 0], x="total_tokens", y="latency_s",
                        hue="Workload_Class", alpha=0.5, palette=CLASS_PALETTE, ax=ax3,
                        edgecolor="none", s=12)
        ax3.set_title("(c) Round Latency vs. Token Volume")
        ax3.set_xlabel("Total Tokens")
        ax3.set_ylabel("Latency (seconds)")
        plt.tight_layout()
        for ext in ("pdf", "png"):
            plt.savefig(os.path.join(output_dir, f"fig6_elephant_mice_profiling.{ext}"))
        plt.close()

    # ------- FIGURE 7: tail latency, contention, s/token -------
    if not df_rounds.empty:
        lat = pd.to_numeric(df_rounds["latency_s"], errors="coerce")
        v = df_rounds[lat > 0].copy()
        lr = v[v["Run_Type"] == "load"]
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(18, 5))
        if not lr.empty:
            p = lr.groupby("Strategy")["latency_s"].quantile([0.5, 0.9, 0.99]).unstack().reset_index()
            pm = p.melt(id_vars="Strategy", value_vars=[0.5, 0.9, 0.99],
                        var_name="Percentile", value_name="Latency_s")
            pm["Percentile"] = pm["Percentile"].map({0.5: "P50", 0.9: "P90", 0.99: "P99"})
            sns.barplot(data=pm, x="Strategy", y="Latency_s", hue="Percentile",
                        order=order(lr), ax=ax1, palette="Blues_d")
            ax1.set_title("(a) Tail Latency Percentiles (Load Runs)")
            ax1.set_xlabel("Placement Strategy")
            ax1.set_ylabel("Latency (seconds)")
            plt.setp(ax1.get_xticklabels(), rotation=30, ha="right")
        sns.boxplot(data=lr, x="Strategy", y="latency_s", hue="Workload_Class",
                    order=order(lr), palette=CLASS_PALETTE, ax=ax2)
        ax2.set_title("(b) Latency Contention by Strategy & Class")
        ax2.set_xlabel("Placement Strategy")
        ax2.set_ylabel("Latency (seconds)")
        plt.setp(ax2.get_xticklabels(), rotation=30, ha="right")
        lr = lr.copy()
        lr["sec_per_token"] = lr["latency_s"] / lr["total_tokens"].replace(0, np.nan)
        sns.boxplot(data=lr, x="Strategy", y="sec_per_token", hue="Workload_Class",
                    order=order(lr), palette=CLASS_PALETTE, ax=ax3)
        ax3.set_title("(c) Normalized Processing Speed (s / Token)")
        ax3.set_xlabel("Placement Strategy")
        ax3.set_ylabel("Seconds per Token")
        plt.setp(ax3.get_xticklabels(), rotation=30, ha="right")
        plt.tight_layout()
        for ext in ("pdf", "png"):
            plt.savefig(os.path.join(output_dir, f"fig7_tail_latency_and_contention.{ext}"))
        plt.close()

    print(f"[SUCCESS] figures written to {os.path.abspath(output_dir)}")



# ==============================================================================
# 3. makespan and per-N latency percentiles
# ==============================================================================
def _fmt(mu, sd, nd=1):
    if mu != mu:
        return "n/a"
    return f"{mu:.{nd}f} ± {sd:.{nd}f}" if sd == sd else f"{mu:.{nd}f}"


def report_tables(df_runs, df_rounds, output_dir, baseline="RandomNode"):
    """Print and save: makespan per (strategy, N), per-N latency percentiles,
    per-class medians, and the change of every strategy against `baseline`.
    Writes CSV (for analysis), Markdown (for reports) and a LaTeX tabular body
    (for the paper) into output_dir/tables/."""
    tdir = os.path.join(output_dir, "tables")
    os.makedirs(tdir, exist_ok=True)
    runs = df_runs[df_runs["Run_Type"] == "load"].copy()
    rounds = df_rounds[df_rounds["Run_Type"] == "load"].copy()
    rounds["latency_s"] = pd.to_numeric(rounds["latency_s"], errors="coerce")
    rounds = rounds[rounds["latency_s"] > 0]

    # --- (a) makespan, window, energy per token: mean ± sd over repetitions ---
    mk = (runs.groupby(["Strategy", "N_Workflows"])
          .agg(reps=("Run_ID", "count"),
               makespan_mean=("Makespan_s", "mean"), makespan_sd=("Makespan_s", "std"),
               window_mean=("Window_s", "mean"),
               spread_mean=("Session_Spread", "mean"),
               jpt_mean=("Joules_Per_Token", "mean"), jpt_sd=("Joules_Per_Token", "std"),
               energy_kj=("Total_Energy_J", lambda x: x.mean() / 1000),
               tps_mean=("Tokens_Per_Sec", "mean"))
          .reset_index())

    # --- (b) per-N percentiles over all rounds of all repetitions ---
    def pct(g):
        v = g["latency_s"]
        return pd.Series({"rounds": len(v), "P50": v.quantile(.5), "P90": v.quantile(.9),
                          "P99": v.quantile(.99), "max": v.max(),
                          "mouse_P50": v[g["Workload_Class"] == "Mouse"].median(),
                          "elephant_P50": v[g["Workload_Class"] == "Elephant"].median(),
                          "mouse_P99": v[g["Workload_Class"] == "Mouse"].quantile(.99),
                          "elephant_P99": v[g["Workload_Class"] == "Elephant"].quantile(.99)})
    per_n = rounds.groupby(["Strategy", "N_Workflows"]).apply(pct).reset_index()
    pooled = rounds.groupby(["Strategy"]).apply(pct).reset_index()
    pooled["N_Workflows"] = "all"

    # --- (c) per-repetition P99, so the P99 itself gets a spread ---
    rep_p99 = (rounds.groupby(["Strategy", "N_Workflows", "Run_ID"])["latency_s"]
               .quantile(.99).groupby(["Strategy", "N_Workflows"]).agg(["mean", "std"])
               .rename(columns={"mean": "P99_rep_mean", "std": "P99_rep_sd"}).reset_index())
    per_n = per_n.merge(rep_p99, on=["Strategy", "N_Workflows"], how="left")

    table = mk.merge(per_n, on=["Strategy", "N_Workflows"], how="outer")
    table.to_csv(os.path.join(tdir, "summary_per_N.csv"), index=False)
    pooled.to_csv(os.path.join(tdir, "latency_pooled.csv"), index=False)

    # --- console + markdown ---
    lines = ["| Strategy | N | reps | makespan (s) | P50 | P90 | P99 | P99/rep (mean ± sd) | max | mouse P50 | elephant P50 | tok/s | J/token |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in table.sort_values(["N_Workflows", "Strategy"]).iterrows():
        lines.append(f"| {r['Strategy']} | {r['N_Workflows']} | {int(r['reps']) if r['reps']==r['reps'] else 'n/a'} | "
                     f"{_fmt(r['makespan_mean'], r['makespan_sd'], 0)} | {r['P50']:.1f} | {r['P90']:.1f} | "
                     f"{r['P99']:.1f} | {_fmt(r['P99_rep_mean'], r['P99_rep_sd'])} | {r['max']:.0f} | "
                     f"{r['mouse_P50']:.1f} | {r['elephant_P50']:.1f} | {r['tps_mean']:.1f} | "
                     f"{_fmt(r['jpt_mean'], r['jpt_sd'], 2)} |")
    for _, r in pooled.iterrows():
        lines.append(f"| {r['Strategy']} | all | – | – | {r['P50']:.1f} | {r['P90']:.1f} | {r['P99']:.1f} | – | "
                     f"{r['max']:.0f} | {r['mouse_P50']:.1f} | {r['elephant_P50']:.1f} | – | – |")

    # --- change against the baseline, per N ---
    delta = ["", f"Change of each strategy against {baseline} (negative = better for latency/energy):", "",
             "| Strategy | N | makespan | P90 | P99 | tok/s | J/token |", "|---|---|---|---|---|---|---|"]
    base = table[table["Strategy"] == baseline].set_index("N_Workflows")
    for _, r in table[table["Strategy"] != baseline].sort_values(["N_Workflows", "Strategy"]).iterrows():
        if r["N_Workflows"] not in base.index:
            continue
        b = base.loc[r["N_Workflows"]]
        ch = lambda a, c: f"{(a - c) / c * 100:+.0f}%" if (a == a and c == c and c) else "n/a"
        delta.append(f"| {r['Strategy']} | {r['N_Workflows']} | {ch(r['makespan_mean'], b['makespan_mean'])} | "
                     f"{ch(r['P90'], b['P90'])} | {ch(r['P99'], b['P99'])} | {ch(r['tps_mean'], b['tps_mean'])} | "
                     f"{ch(r['jpt_mean'], b['jpt_mean'])} |")
    md = "\n".join(lines + delta)
    open(os.path.join(tdir, "summary.md"), "w").write(md + "\n")
    print("\n=== makespan and per-N latency ===\n" + md + "\n")
    tex = []
    for n in sorted(table["N_Workflows"].unique()):
        for _, r in table[table["N_Workflows"] == n].sort_values("Strategy").iterrows():
            tex.append(f"{r['Strategy']} & {n} & {r['makespan_mean']:.0f} & {r['P50']:.1f} & "
                       f"{r['P90']:.1f} & {r['P99']:.1f} & {r['tps_mean']:.1f} & {r['jpt_mean']:.2f} \\\\")
        tex.append("\\midrule")
    open(os.path.join(tdir, "table_per_N.tex"), "w").write("\n".join(tex[:-1]) + "\n")
    print(f"[INFO] tables written to {tdir}/ (summary.md, summary_per_N.csv, table_per_N.tex)")
    return table


def plot_per_n(table, output_dir):
    """Figure 8: per-N latency percentiles and campaign makespan."""
    ho = [s for s in STRAT_ORDER if s in table["Strategy"].unique()] + \
         [s for s in table["Strategy"].unique() if s not in STRAT_ORDER]
    fig, axes = plt.subplots(1, 4, figsize=(18, 4.2))
    for ax, col, title in zip(axes, ["P50", "P90", "P99", "makespan_mean"],
                              ["(a) Round latency P50", "(b) Round latency P90",
                               "(c) Round latency P99", "(d) Campaign makespan"]):
        sns.barplot(data=table, x="N_Workflows", y=col, hue="Strategy", hue_order=ho,
                    palette=STRAT_PALETTE, ax=ax)
        ax.set_title(title)
        ax.set_xlabel("Concurrent Agents N")
        ax.set_ylabel("seconds")
        if ax is not axes[0]:
            ax.get_legend().remove()
    plt.tight_layout()
    for ext in ("pdf", "png"):
        plt.savefig(os.path.join(output_dir, f"fig8_per_N_latency_makespan.{ext}"))
    plt.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EDGELESS campaign figures and tables")
    ap.add_argument("--campaign", default="/home/EDGELESS-PROJECT/results/campaign")
    ap.add_argument("--out", default="/home/EDGELESS-PROJECT/results/plots")
    ap.add_argument("--baseline", default="RandomNode",
                    help="strategy the others are compared against in the change table")
    ap.add_argument("--only", default="",
                    help="comma-separated strategies to keep, CAP,RandomNode")
    args = ap.parse_args()

    if not os.path.exists(args.campaign):
        raise SystemExit(f"[ERROR] campaign directory not found: {args.campaign}")
    df_runs, df_nodes, df_rounds = ingest_campaign_data(args.campaign)
    if df_runs.empty:
        raise SystemExit("[ERROR] found files but no valid paired runs.")
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        df_runs = df_runs[df_runs["Strategy"].isin(keep) | (df_runs["Run_Type"] != "load")]
        df_nodes = df_nodes[df_nodes["Strategy"].isin(keep) | (df_nodes["Run_Type"] != "load")]
        df_rounds = df_rounds[df_rounds["Strategy"].isin(keep) | (df_rounds["Run_Type"] != "load")]
    print(f"[INFO] processed {len(df_runs)} runs.")
    generate_all_plots(df_runs, df_nodes, df_rounds, args.out)
    table = report_tables(df_runs, df_rounds, args.out, baseline=args.baseline)
    plot_per_n(table, args.out)
