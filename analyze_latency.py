import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Load dataset
df = pd.read_csv("dataset/performance_samples.csv")

df.columns = [
    "experiment",
    "identifier",
    "metric",
    "timestamp",
    "value"
]

# Keep only transfer + execution
df = df[df["metric"].isin([
    "function_transfer_time",
    "function_execution_time"
])]

# Convert seconds → milliseconds
df["latency_ms"] = df["value"] * 1000

# Combine transfer + execution per identifier & timestamp bucket
# (Approximate total latency)
grouped = df.groupby(["experiment"])["latency_ms"]

mean = grouped.mean()
std = grouped.std()
count = grouped.count()

ci95 = 1.96 * std / np.sqrt(count)

print("\n=== RESULTS ===")
for exp in mean.index:
    print(f"{exp}")
    print(f"  Mean latency: {mean[exp]:.4f} ms")
    print(f"  95% CI: ±{ci95[exp]:.4f} ms")
    print()

# Plot
plt.figure(figsize=(8,5))
plt.bar(mean.index, mean.values, yerr=ci95.values)
plt.ylabel("Latency (ms)")
plt.title("End-to-End Latency with 95% Confidence Interval")
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()