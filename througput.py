import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

files = glob.glob("res/*.csv")

rows = []

for file in files:

    m = re.search(r"f(\d+)_p([0-9]+(?:\.[0-9]+)?)_r(\d+)\.csv", file)
    print("Processing:", file)
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

    df.columns = ["label", "function_id", "metric", "timestamp", "value"]

    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

    df = df.dropna(subset=["value", "timestamp"])

    if df.empty:
        continue

    # =============================
    # 📊 THROUGHPUT CALCULATION
    # =============================

    exec_df = df[df["metric"] == "function_execution_time"]
    N = len(exec_df)

    if N == 0:
        continue

    T_min = df["timestamp"].min()
    T_max = df["timestamp"].max()

    duration = T_max - T_min

    if duration <= 0:
        continue

    num_functions = df["function_id"].nunique()

    throughput = (N / duration) / num_functions

    rows.append({
        "f": f,
        "p": p,
        "run": r,
        "throughput": throughput
    })

df = pd.DataFrame(rows)

if df.empty:
    print("No valid data found!")
    exit()

print(df.groupby("p").size())
print("Unique p values found:", sorted(df["p"].unique()))

summary = df.groupby(["f", "p"]).agg(
    mean=("throughput", "mean"),
    std=("throughput", "std"),
    n=("throughput", "count")
).reset_index()

summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"]))

summary.to_csv("throughput_summary_with_ci.csv", index=False)

print("\n✅ Throughput Summary with CI:")
print(summary)

baseline = summary[summary["p"] == 0.0][["f", "mean"]].rename(columns={"mean": "baseline"})
summary = summary.merge(baseline, on="f")

summary["relative_diff_%"] = (
    (summary["mean"] - summary["baseline"]) / summary["baseline"]
) * 100

plt.figure(figsize=(10, 6))

colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

for i, p in enumerate(sorted(summary["p"].unique())):
    subset = summary[summary["p"] == p].sort_values("f")

    plt.plot(
        subset["f"], subset["mean"],
        marker="o", linewidth=2, markersize=8,
        color=colors[i], label=f"p = {p}"
    )

    plt.errorbar(
        subset["f"], subset["mean"],
        yerr=subset["ci95"],
        fmt="none", capsize=5,
        elinewidth=2, color=colors[i]
    )

plt.xlabel("Number of Functions (f)")
plt.ylabel("Throughput per Function (req/sec)")
plt.title("Throughput per Function vs Workflow Size (95% CI)")
plt.grid(True, linestyle="--", alpha=0.6)
plt.legend(title="Write Probability")

plt.tight_layout()
plt.savefig("throughput_absolute.png", dpi=400)
plt.show()
plt.figure(figsize=(10, 6))

for i, p in enumerate(sorted(summary["p"].unique())):
    subset = summary[summary["p"] == p].sort_values("f")

    plt.plot(
        subset["f"], subset["relative_diff_%"],
        marker="o", linewidth=2, markersize=8,
        color=colors[i], label=f"p = {p}"
    )

plt.axhline(0, linestyle="--", linewidth=1)

plt.xlabel("Number of Functions (f)")
plt.ylabel("Throughput Difference (%)")
plt.title("Relative Throughput vs Baseline (p = 0)")
plt.grid(True, linestyle="--", alpha=0.6)
plt.legend(title="Write Probability")

plt.tight_layout()
plt.savefig("throughput_relative.png", dpi=400)
plt.show()