#!/usr/bin/env python3

import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt
import os

# ==========================================
# NODE ID -> HUMAN READABLE NAME
# ==========================================
NODE_MAP = {
    "e4a1c2b3-7d8f-4c9a-b123-8f9e1d2c3a45": "rpi-03",
    "9b2f3c7a-8c2e-4f0a-b8d1-1c2d3e4f5678": "rpi-02",
}

# ==========================================
# RESULTS DIRECTORY
# ==========================================
RESULTS_GLOB = "results-20k-1/f*_r*/health_status.csv"

# ==========================================
# FIND ALL HEALTH FILES
# ==========================================
files = glob.glob(RESULTS_GLOB)
print(f"Found {len(files)} health files")

rows = []

# ==========================================
# PROCESS EACH RUN
# ==========================================
for file in files:
    # Extract workload (f) and run number
    m = re.search(r"f(\d+)_r(\d+)", file)
    if not m:
        continue

    f = int(m.group(1))
    run = int(m.group(2))

    run_dir = os.path.dirname(file)

    # ======================================
    # PROCESS health_status.csv (RPIs)
    # ======================================
    try:
        df = pd.read_csv(
            file,
            header=None,
            on_bad_lines="skip"
        )
    except Exception as e:
        print(f"Cannot read {file}: {e}")
        continue

    # Need at least 24 columns
    if df.shape[1] < 24:
        print(f"Skipping malformed file: {file}")
        continue

    # Keep only first 24 columns
    df = df.iloc[:, :24]

    # Assign column names
    df.columns = [
        "test", "timestamp", "node_id",
        "mem_free", "mem_used", "mem_available",
        "proc_cpu_usage", "proc_memory", "proc_vmemory",
        "load_avg_1", "load_avg_5", "load_avg_15",
        "tot_rx_bytes", "tot_rx_pkts", "tot_rx_errs",
        "tot_tx_bytes", "tot_tx_pkts", "tot_tx_errs",
        "disk_free_space", "disk_tot_reads", "disk_tot_writes",
        "gpu_load_perc", "gpu_temp_cels", "active_power"
    ]

    # Keep only the Raspberry Pi nodes
    df = df[df["node_id"].isin(NODE_MAP.keys())]

    if not df.empty:
        # Convert CPU to numeric
        df["proc_cpu_usage"] = pd.to_numeric(
            df["proc_cpu_usage"],
            errors="coerce"
        )

        df = df.dropna(subset=["proc_cpu_usage"])

        if not df.empty:
            # Map UUIDs to names
            df["node"] = df["node_id"].map(NODE_MAP)

            # Average CPU per node during this run
            avg_cpu = (
                df.groupby("node")["proc_cpu_usage"]
                  .mean()
            )

            # Save results
            for node, cpu in avg_cpu.items():
                rows.append({
                    "f": f,
                    "run": run,
                    "node": node,
                    "cpu": cpu
                })

    # ======================================
    # PROCESS mohamed1_cpu.csv (Desktop)
    # ======================================
    cpu_file = os.path.join(run_dir, "mohamed1_cpu.csv")

    if os.path.exists(cpu_file):
        try:
            cpu_df = pd.read_csv(cpu_file)
        except Exception as e:
            print(f"Cannot read {cpu_file}: {e}")
            cpu_df = None

        if cpu_df is not None:
            # Detect CPU column
            if "cpu" in cpu_df.columns:
                cpu_col = "cpu"
            elif "cpu_usage" in cpu_df.columns:
                cpu_col = "cpu_usage"
            else:
                cpu_col = None
                print(f"No CPU column found in {cpu_file}")

            if cpu_col is not None:
                # Convert to numeric
                cpu_df[cpu_col] = pd.to_numeric(
                    cpu_df[cpu_col],
                    errors="coerce"
                )

                cpu_df = cpu_df.dropna(subset=[cpu_col])

                if not cpu_df.empty:
                    # Average CPU during this run
                    avg_cpu = cpu_df[cpu_col].mean()

                    rows.append({
                        "f": f,
                        "run": run,
                        "node": "mohamed-1",
                        "cpu": avg_cpu
                    })
    else:
        print(f"Missing {cpu_file}")

# ==========================================
# BUILD DATAFRAME
# ==========================================
results = pd.DataFrame(rows)

if results.empty:
    print("No CPU data found.")
    exit()

# ==========================================
# SUMMARY OVER RUNS
# ==========================================
summary = (
    results.groupby(["f", "node"])
           .agg(
               mean=("cpu", "mean"),
               std=("cpu", "std"),
               n=("cpu", "count")
           )
           .reset_index()
)

# 95% confidence interval
summary["ci95"] = 1.96 * (
    summary["std"].fillna(0) / np.sqrt(summary["n"])
)

print("\nAverage CPU Summary Per Node:")
print(summary)

summary.to_csv("cpu_per_node_average_summary.csv", index=False)

# ==========================================
# PLOT
# ==========================================
plt.figure(figsize=(10, 6))

# Fixed plotting order
node_order = ["mohamed-1", "rpi-02", "rpi-03"]

for node in node_order:
    if node not in summary["node"].values:
        continue

    sub = (
        summary[summary["node"] == node]
        .sort_values("f")
    )

    plt.plot(
        sub["f"],
        sub["mean"],
        marker="o",
        linewidth=3,
        label=node
    )

    plt.errorbar(
        sub["f"],
        sub["mean"],
        yerr=sub["ci95"],
        fmt="none",
        capsize=4
    )

plt.xlabel("Number of Lambda Functions (f)")
plt.ylabel("Average CPU Usage (%)")
plt.title("Average CPU Usage per Node vs Workload")
plt.grid(True)
plt.legend()
plt.tight_layout()

plt.savefig("cpu_per_node_vs_workload_average-20k-1.png", dpi=400)
plt.show()
