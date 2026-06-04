import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# ==========================================================
# CONFIGURATION
# ==========================================================
files = glob.glob("results-50k-1/f*_r*/health_status.csv")
print(f"Found {len(files)} health files")

rows = []

# ==========================================================
# PROCESS EACH HEALTH FILE
# ==========================================================
for file in files:

    # Extract workload (f) and repetition (r)
    m = re.search(r"f(\d+)_r(\d+)", file)
    if not m:
        continue

    f = int(m.group(1))
    r = int(m.group(2))

    # ------------------------------------------------------
    # Read CSV
    # ------------------------------------------------------
    try:
        df = pd.read_csv(file)
    except:
        continue

    # ------------------------------------------------------
    # Handle files without header
    # ------------------------------------------------------
    if "proc_cpu_usage" not in df.columns:
        try:
            df = pd.read_csv(file, header=None)
        except:
            continue

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

    # ------------------------------------------------------
    # Validate CPU column
    # ------------------------------------------------------
    if "proc_cpu_usage" not in df.columns:
        print(f"Missing CPU column in {file}")
        continue

    # Convert CPU values to numeric
    df["proc_cpu_usage"] = pd.to_numeric(
        df["proc_cpu_usage"],
        errors="coerce"
    )

    # Remove invalid rows
    df = df.dropna(subset=["proc_cpu_usage"])

    if df.empty:
        continue

    # ------------------------------------------------------
    # Align timestamps (same second = same sampling instant)
    # ------------------------------------------------------
    df["tbin"] = df["timestamp"].round(0)

    # ------------------------------------------------------
    # Sum CPU across all nodes
    # Example:
    #   rpi-02 = 395%
    #   rpi-03 = 400%
    #   cluster = 795%
    # ------------------------------------------------------
    cluster_cpu = (
        df.groupby("tbin")["proc_cpu_usage"]
          .sum()
    )

    if cluster_cpu.empty:
        continue

    # ------------------------------------------------------
    # PEAK CPU:
    # Use only the top 20% of cluster CPU samples
    # to focus on the active execution period.
    # ------------------------------------------------------
    cpu_sorted = cluster_cpu.sort_values(ascending=False)

    # At least 3 samples
    k = max(3, int(len(cpu_sorted) * 0.20))

    # Average of highest-CPU samples
    peak_cpu = cpu_sorted.iloc[:k].mean()

    # Store result for this run
    rows.append({
        "f": f,
        "run": r,
        "cpu": peak_cpu
    })

# ==========================================================
# BUILD DATAFRAME
# ==========================================================
df = pd.DataFrame(rows)

if df.empty:
    print("❌ No CPU data found!")
    exit()

# ==========================================================
# SUMMARY ACROSS RUNS
# ==========================================================
summary = df.groupby("f").agg(
    mean=("cpu", "mean"),
    std=("cpu", "std"),
    n=("cpu", "count")
).reset_index()

# 95% Confidence Interval
summary["ci95"] = 1.96 * (
    summary["std"] / np.sqrt(summary["n"])
)

# Convert to fraction of total cluster capacity (800%)
summary["utilization_pct"] = (
    summary["mean"] / 800.0 * 100.0
)

# ==========================================================
# PRINT RESULTS
# ==========================================================
print("\n✅ Peak Cluster CPU Summary:")
print(summary)

# Save CSV
summary.to_csv("cpu_summary.csv", index=False)

# ==========================================================
# PLOT
# ==========================================================
plt.figure(figsize=(10, 6))

plt.plot(
    summary["f"],
    summary["mean"],
    marker="o",
    linewidth=3
)

plt.errorbar(
    summary["f"],
    summary["mean"],
    yerr=summary["ci95"],
    fmt="none",
    capsize=5
)

# Saturation line (2 nodes × 4 cores = 800%)
plt.axhline(
    800,
    linestyle="--",
    linewidth=2,
    label="Cluster Capacity (800%)"
)

plt.xlabel("Number of Lambda Functions (f)")
plt.ylabel("Peak Cluster CPU Usage (%)")
plt.title("Peak Cluster CPU Usage vs Workload")
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.savefig("cpu_lambda_50k_peak.png", dpi=400)
plt.show()
