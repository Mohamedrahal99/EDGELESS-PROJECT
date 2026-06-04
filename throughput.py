import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# =============================
# 📂 LOAD FILES
# =============================

files = glob.glob("res/*.csv")

rows = []

for file in files:

    # extract parameters from filename
    m = re.search(, file)
    if not m:
        continue

    f = int(m.group(1))
    p = float(m.group(2))
    r = int(m.group(3))

    try:
        df = pd.read_csv(file, header=None)
    except:
        print(f"Skipping corrupted file: {file}")
        continue

    # assign columns
    df.columns = ["label", "function_id", "metric", "timestamp", "value"]

    # convert types
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

    # drop invalid rows
    df = df.dropna(subset=["value", "timestamp"])

    if df.empty:
        continue

    # =============================
    # 📊 THROUGHPUT CALCULATION
    # =============================

    # N = number of execution events
    exec_df = df[df["metric"] == "function_execution_time"]
    N = len(exec_df)

    if N == 0:
        continue

    # time window
    T_min = df["timestamp"].min()
    T_max = df["timestamp"].max()

    duration = T_max - T_min

    if duration <= 0:
        continue

    # number of unique functions
    num_functions = df["function_id"].nunique()

    # 👉 throughput per function
    throughput = (N / duration) / num_functions

    rows.append({
        "f": f,
        "p": p,
        "run": r,
        "throughput": throughput
    })

# =============================
# 📦 CREATE DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("No valid data found!")
    exit()

# =============================
# 🔥 STATISTICS
# =============================

summary = df.groupby(["f", "p"]).agg(
    mean=("throughput", "mean"),
    std=("throughput", "std"),
    n=("throughput", "count")
).reset_index()

# 95% confidence interval
summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"]))

summary.to_csv("throughput_summary_with_ci.csv", index=False)

print("\n✅ Throughput Summary with CI:")
print(summary)

# ============≈=================
# 📊 PLOT
# =============================

plt.figure(figsize=(10, 6))

colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

for i, p in enumerate(sorted(summary["p"].unique())):

    subset = summary[summary["p"] == p].sort_values("f")

    x = subset["f"]
    y = subset["mean"]
    yerr = subset["ci95"]

    # line
    plt.plot(x, y, marker="o", linewidth=3, color=colors[i], label=f"p = {p}")

    # error bars
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
plt.ylabel("Throughput per Function (req/sec)", fontsize=13)
plt.title("Throughput per Function vs Workflow Size (95% CI)", fontsize=15)

plt.grid(True, linestyle="--", alpha=0.6)
plt.legend(title="Write Probability")

plt.tight_layout()
plt.savefig("throughput_per_function.png", dpi=400)
plt.show()
