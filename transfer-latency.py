import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# =============================
# 📂 LOAD FILES
# =============================

files = glob.glob("results-p1/*.csv")

rows = []

for file in files:

    # match filenames like: f10_r0.csv
    m = re.search(r"f(\d+)_r(\d+)\.csv", file)
    if not m:
        print(f"Skipping unmatched file: {file}")
        continue

    f = int(m.group(1))
    r = int(m.group(2))

    # =============================
    # 📄 READ CSV
    # =============================

    try:
        df = pd.read_csv(file, header=None)
    except Exception as e:
        print(f"Skipping corrupted file: {file} ({e})")
        continue

    df.columns = ["label", "function_id", "metric", "timestamp", "value"]

    # clean numeric values
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    df = df.dropna(subset=["timestamp", "value"])

    if df.empty:
        continue

    # =============================
    # 🧠 TRANSACTION SEGMENTATION
    # =============================

    # normalize timestamps
    df["t0"] = df["timestamp"] - df["timestamp"].min()

    # each ~1 second = one workflow execution
    df["tx_id"] = df["t0"].astype(int)

    transactions = []

    for tx, group in df.groupby("tx_id"):

        if group.empty:
            continue

        start = group["timestamp"].min()
        end = group["timestamp"].max()

        latency = end - start

        # filter tiny noise windows
        if latency > 0.001:
            transactions.append(latency)

    if len(transactions) == 0:
        continue

    avg_tx_latency = np.mean(transactions)

    rows.append({
        "f": f,
        "run": r,
        "transaction_latency": avg_tx_latency
    })

# =============================
# 📊 BUILD DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("No valid data found!")
    exit()

# =============================
# 📊 STATISTICS
# =============================

summary = df.groupby("f").agg(
    mean=("transaction_latency", "mean"),
    std=("transaction_latency", "std"),
    n=("transaction_latency", "count")
).reset_index()

# convert to milliseconds
summary["mean_ms"] = summary["mean"] * 1000

# 95% confidence interval
summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"])) * 1000

# save results
summary.to_csv("transaction_summary.csv", index=False)

print("\n✅ Transaction Latency Summary:")
print(summary)

# =============================
# 📈 PLOT
# =============================

plt.figure(figsize=(10,6))

plt.plot(
    summary["f"],
    summary["mean_ms"],
    marker="o",
    linewidth=3,
    label="Transaction latency"
)

plt.errorbar(
    summary["f"],
    summary["mean_ms"],
    yerr=summary["ci95"],
    fmt="none",
    capsize=5,
    elinewidth=2
)

plt.xlabel("Number of Functions (f)", fontsize=13)
plt.ylabel("Transaction Latency (ms)", fontsize=13)
plt.title("Transaction Latency vs Workflow Size (95% CI)", fontsize=15)

plt.grid(True, linestyle="--", alpha=0.6)
plt.legend()

plt.tight_layout()

plt.savefig("transaction_latency.png", dpi=400)
plt.show()
