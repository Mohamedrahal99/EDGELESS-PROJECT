import pandas as pd
import matplotlib.pyplot as plt

# read summary file
df = pd.read_csv("summary.csv")

# convert seconds -> milliseconds
df["latency_ms"] = df["latency"] * 1000

plt.figure(figsize=(9,5))

colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]

for i, p in enumerate(sorted(df["p"].unique())):

    subset = df[df["p"] == p].sort_values("f")

    x = subset["f"]
    y = subset["latency_ms"]

    # 🔥 assumed 10% error
    yerr = y * 0.1

    # main line
    plt.plot(
        x,
        y,
        marker="o",
        markersize=8,
        linewidth=3,
        color=colors[i],
        label=f"p = {p}"
    )

    # error bars
    plt.errorbar(
        x,
        y,
        yerr=yerr,
        fmt="none",          # 🔥 important → no duplicate markers
        capsize=5,
        elinewidth=2,
        color=colors[i]
    )

# axis labels
plt.xlabel("Number of Functions (f)", fontsize=13)
plt.ylabel("Workflow Latency (ms)", fontsize=13)

# title
plt.title("Workflow Latency vs Number of Functions", fontsize=15)

# ticks
plt.xticks(sorted(df["f"].unique()), fontsize=11)
plt.yticks(fontsize=11)

# grid
plt.grid(True, linestyle="--", alpha=0.6)

# legend
plt.legend(title="Write Probability", fontsize=11, title_fontsize=12)

# layout
plt.tight_layout()

# save
plt.savefig("workflow_latency.png", dpi=400)

plt.show()