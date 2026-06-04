import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# =============================
# 📂 LOAD FILES
# =============================

files = glob.glob("results/f*_r*_app.csv")

print(f"Found {len(files)} app files")

rows = []

# =============================
# 📊 PROCESS EACH FILE
# =============================

for file in files:

    # extract f and run
    m = re.search(r"f(\d+)_r(\d+)_app\.csv", file)
    if not m:
        continue

    f = int(m.group(1))
    r = int(m.group(2))

    try:
        df = pd.read_csv(file, header=None)
    except Exception as e:
        print(f"Error reading {file}: {e}")
        continue

    if df.empty:
        continue

    # assign columns
    df.columns = ["test", "function_id", "target", "timestamp", "value"]

    # clean timestamp
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])

    # =============================
    # 🔍 EXTRACT EVENTS
    # =============================

    tbegin_df = df[df["target"] == "tbegin"]
    tend_df = df[df["target"] == "tend"]

    if tbegin_df.empty or tend_df.empty:
        continue

    # rename for merge
    tbegin_df = tbegin_df.rename(columns={"timestamp": "tbegin"})
    tend_df = tend_df.rename(columns={"timestamp": "tend"})

    # =============================
    # 🔗 MATCH TRANSACTIONS
    # =============================
    # match using function_id + tx_id (value column)

    merged = pd.merge(
        tbegin_df[["function_id", "value", "tbegin"]],
        tend_df[["function_id", "value", "tend"]],
        on=["function_id", "value"]
    )

    if merged.empty:
        continue

    # =============================
    # 📊 COMPUTE LATENCY
    # =============================

    merged["latency"] = merged["tend"] - merged["tbegin"]

    # filter bad values
    merged = merged[(merged["latency"] > 0) & (merged["latency"] < 100)]

    if merged.empty:
        continue

    rows.append({
        "f": f,
        "run": r,
        "latency": merged["latency"].mean()
    })

# =============================
# 📊 BUILD DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("❌ No latency data found")
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

print("\n✅ Latency Summary:")
print(summary)

# =============================
# 📈 PLOT
# =============================

plt.figure(figsize=(10,6))

x = summary["f"]
y = summary["mean_ms"]
yerr = summary["ci95"]

plt.plot(x, y, marker="o", linewidth=3)
plt.errorbar(x, y, yerr=yerr, fmt="none", capsize=5)

plt.xlabel("Number of Functions (f)")
plt.ylabel("Transaction Latency (ms)")
plt.title("End-to-End Latency (tend - tbegin) vs Workflow Size")

plt.grid(True, linestyle="--", alpha=0.6)

plt.tight_layout()
plt.savefig("last_latency.png", dpi=400)
plt.show()
