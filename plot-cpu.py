import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

files = glob.glob("results-50k-1/f*_r*/health_status.csv")

print(f"Found {len(files)} health files")

rows = []

for file in files:

    m = re.search(r"f(\d+)_r(\d+)", file)
    if not m:
        continue

    f = int(m.group(1))
    r = int(m.group(2))

    try:
        df = pd.read_csv(file)
    except:
        continue

    # =============================
    # HANDLE HEADERLESS FILES
    # =============================

    if "proc_cpu_usage" not in df.columns:

        try:
            df = pd.read_csv(file, header=None)
        except:
            continue

        df.columns = [
            "test","timestamp","node_id",
            "mem_free","mem_used","mem_available",
            "proc_cpu_usage","proc_memory","proc_vmemory",
            "load_avg_1","load_avg_5","load_avg_15",
            "tot_rx_bytes","tot_rx_pkts","tot_rx_errs",
            "tot_tx_bytes","tot_tx_pkts","tot_tx_errs",
            "disk_free_space","disk_tot_reads","disk_tot_writes",
            "gpu_load_perc","gpu_temp_cels","active_power"
        ]

    # =============================
    # CPU COLUMN
    # =============================

    if "proc_cpu_usage" not in df.columns:
        print(f"Missing CPU in {file}")
        continue

    df["proc_cpu_usage"] = pd.to_numeric(
        df["proc_cpu_usage"],
        errors="coerce"
    )

    df = df.dropna(subset=["proc_cpu_usage"])

    if df.empty:
        continue

    # =============================
    # ALIGN TIMESTAMPS
    # =============================

    # round timestamps to nearest second
    df["tbin"] = df["timestamp"].round(0)

    # =============================
    # SUM CPU ACROSS NODES
    # =============================

    cluster_cpu = (
        df.groupby("tbin")["proc_cpu_usage"]
          .sum()
    )

    # =============================
    # MEAN CLUSTER CPU OVER TIME
    # =============================

    cpu_mean = cluster_cpu.mean()

    rows.append({
        "f": f,
        "run": r,
        "cpu": cpu_mean
    })

# =============================
# BUILD DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("❌ No CPU data found!")
    exit()

# =============================
# SUMMARY OVER RUNS
# =============================

summary = df.groupby("f").agg(
    mean=("cpu", "mean"),
    std=("cpu", "std"),
    n=("cpu", "count")
).reset_index()

summary["ci95"] = 1.96 * (
    summary["std"] / np.sqrt(summary["n"])
)

print("\n✅ CPU Summary:")
print(summary)

summary.to_csv("cpu_summary.csv", index=False)

# =============================
# PLOT
# =============================

plt.figure(figsize=(10,6))

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

plt.xlabel("Number of Functions (f)")
plt.ylabel("Cluster CPU Usage (%)")
plt.title("Cluster CPU Usage vs Workload")

plt.grid(True)

plt.tight_layout()
plt.savefig("cpu_lambda-50k-1.png", dpi=400)
plt.show()
