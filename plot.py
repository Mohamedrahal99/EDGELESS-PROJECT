import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# =============================
# 📂 LOAD FILES FROM FOLDERS
# =============================

files = glob.glob("results-50k-1/f*_r*/performance_samples.csv")

print(f"Found {len(files)} performance files")

rows = []

# =============================
# 📊 PROCESS FILES
# =============================

for file in files:

    # extract f and run
    m = re.search(r"f(\d+)_r(\d+)", file)
    if not m:
        continue

    f = int(m.group(1))
    r = int(m.group(2))

    # =============================
    # 📄 READ CSV (NO HEADER)
    # =============================

    try:
        df = pd.read_csv(file, header=None)
    except Exception as e:
        print(f"Skipping {file}: {e}")
        continue

    # assign correct columns
    df.columns = ["test", "identifier", "metric", "timestamp", "value"]

    # convert numeric safely
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    if df.empty:
        continue

    # =============================
    # 🔄 FILTER METRICS
    # =============================

    df = df[df["metric"].isin([
        "function_execution_time",
        "function_transfer_time"
    ])]

    if df.empty:
        continue

    # =============================
    # 🔄 PIVOT METRICS
    # =============================

    pivot = df.pivot_table(
        index="identifier",
        columns="metric",
        values="value",
        aggfunc="mean"
    )

    if pivot.empty:
        continue

    # handle missing columns safely
    exec_time = pivot["function_execution_time"] if "function_execution_time" in pivot else 0
    transfer_time = pivot["function_transfer_time"] if "function_transfer_time" in pivot else 0

    pivot["latency"] = exec_time + transfer_time

    # =============================
    # 📊 STORE RESULT
    # =============================

    rows.append({
        "f": f,
        "run": r,
        "latency": pivot["latency"].mean()
    })

# =============================
# 📊 BUILD DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("❌ No valid data found!")
    exit()

# =============================
# 📊 STATISTICS
# =============================

summary = df.groupby("f").agg(
    mean=("latency", "mean"),
    std=("latency", "std"),
    n=("latency", "count")
).reset_index()

summary["mean_ms"] = summary["mean"] * 1000
summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"])) * 1000

print("\n✅ Summary:")
print(summary)

summary.to_csv("function_latency_summary.csv", index=False)

# =============================
# 📈 PLOT
# =============================

plt.figure(figsize=(10,6))

plt.plot(summary["f"], summary["mean_ms"], marker="o", linewidth=3)
plt.errorbar(summary["f"], summary["mean_ms"],
             yerr=summary["ci95"], fmt="none", capsize=5)

plt.xlabel("Number of Functions (f)", fontsize=13)
plt.ylabel("Latency (ms)", fontsize=13)
plt.title("Function Latency vs Workload Size", fontsize=15)

plt.grid(True, linestyle="--", alpha=0.6)

plt.tight_layout()
plt.savefig("lambda_latency-50k-1.png", dpi=400)
plt.show()
