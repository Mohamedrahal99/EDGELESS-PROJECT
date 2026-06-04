import pandas as pd
import glob
import re
import numpy as np
import matplotlib.pyplot as plt

# =============================
# 📂 LOAD FILES (MAIN METRICS)
# =============================

files = [
    f for f in glob.glob("results/f*_r*.csv")
    if "_cpu" not in f and "_app" not in f
]

print(f"Found {len(files)} metric files")

rows = []

# =============================
# 📊 PROCESS FILES
# =============================

for file in files:

    m = re.search(r"f(\d+)_r(\d+)\.csv", file)
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
    df.columns = ["label", "function_id", "metric", "timestamp", "value"]

    # numeric values only
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    if df.empty:
        continue

    # =============================
    # 🔑 FILTER TRANSFER TIME
    # =============================

    transfer = df[df["metric"] == "function_transfer_time"]

    if transfer.empty:
        continue

    # average per function
    per_function = transfer.groupby("function_id")["value"].mean()

    rows.append({
        "f": f,
        "run": r,
        "transfer": per_function.mean()
    })

# =============================
# 📊 BUILD DATAFRAME
# =============================

df = pd.DataFrame(rows)

if df.empty:
    print("❌ No transfer data found")
    exit()

# =============================
# 📊 STATISTICS
# =============================

summary = df.groupby("f").agg(
    mean=("transfer", "mean"),
    std=("transfer", "std"),
    n=("transfer", "count")
).reset_index()

# convert to ms
summary["mean_ms"] = summary["mean"] * 1000

# CI95
summary["ci95"] = 1.96 * (summary["std"] / np.sqrt(summary["n"])) * 1000

print("\n📊 Transfer Time Summary:")
print(summary)

# =============================
# 📈 PLOT
# =============================

plt.figure(figsize=(10,6))

x = summary["f"]
y = summary["mean_ms"]
yerr = summary["ci95"]

plt.plot(x, y, marker="o", linewidth=2)
plt.errorbar(x, y, yerr=yerr, fmt="none", capsize=5)

plt.xlabel("Number of Functions (f)")
plt.ylabel("Function Transfer Time (ms)")
plt.title("Function Transfer Time vs Number of Functions")

plt.grid(True)

plt.tight_layout()
plt.savefig("transfer_time.png", dpi=300)
plt.show()

# =============================
# 📊 PRINT RESULTS
# =============================

print("\n📊 Results:")
for _, row in summary.iterrows():
    print(f"f={int(row['f'])} → {row['mean_ms']:.2f} ms")
