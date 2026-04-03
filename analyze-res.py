import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

files = glob.glob("res/*.csv")

rows = []

for file in files:

    m = re.search(r"f(\d+)_p([0-9.]+)_r(\d+)\.csv", file)
    if not m:
        continue

    f = int(m.group(1))
    p = float(m.group(2))
    r = int(m.group(3))  # run id

    try:
        df = pd.read_csv(file, header=None)
    except:
        print(f"Skipping corrupted file: {file}")
        continue

    df.columns = ["label", "function_id", "metric", "timestamp", "value"]

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    if df.empty:
        continue

    pivot = df.pivot_table(
        index="function_id",
        columns="metric",
        values="value",
        aggfunc="sum"
    )

    exec_time = pivot.get("function_execution_time", pd.Series(0, index=pivot.index))
    transfer_time = pivot.get("function_transfer_time", pd.Series(0, index=pivot.index))

    pivot["latency"] = exec_time + transfer_time

    # 🔥 N = number of execution events
    N = len(df[df["metric"] == "function_execution_time"])
    if N == 0:
        continue

    avg_latency = pivot["latency"].sum() / N

    rows.append({
        "f": f,
        "p": p,
        "run": r,
        "latency": avg_latency
    })

df = pd.DataFrame(rows)

if df.empty:
    print("No valid data found!")
    exit()

# =============================
# 🔥 STATISTICS
# =============================

summary = df.groupby(["f", "p"]).agg(
    mean=("latency", "mean"),
    std=("latency", "std"),
    n=("latency", "count")
).reset_index()

# convert to ms
summary["mean_ms"] = summary["mean"] * 1000

# 🔥 95% confidence interval
summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"])) * 1000

summary.to_csv("summary_with_ci.csv", index=False)

print("\n✅ Summary with CI:")
print(summary)

# =============================
# 📊 PLOT
# =============================

plt.figure(figsize=(10,6))

colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

for i, p in enumerate(sorted(summary["p"].unique())):

    subset = summary[summary["p"] == p].sort_values("f")

    x = subset["f"]
    y = subset["mean_ms"]
    yerr = subset["ci95"]

    # line
    plt.plot(x, y, marker="o", linewidth=3, color=colors[i], label=f"p = {p}")

    # 🔥 REAL 95% CI
    plt.errorbar(
        x,
        y,
        yerr=yerr,
        fmt="none",
        capsize=5,
        elinewidth=2,
        color=colors[i]
    )

plt.xlabel("Number of Functions (f)", fontsize=13)
plt.ylabel("Function Latency (ms)", fontsize=13)
plt.title("Latency vs Workflow Size (95% Confidence Interval)", fontsize=15)

plt.grid(True, linestyle="--", alpha=0.6)
plt.legend(title="Write Probability")

plt.tight_layout()
plt.savefig("latency_with_ci.png", dpi=400)
plt.show()