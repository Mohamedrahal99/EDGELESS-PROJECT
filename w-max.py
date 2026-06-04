#!/usr/bin/env python3

"""
Compute and plot:

E[ W_max ]

where for each run r:

1. Group waiting_size samples by function UUID (identifier)
2. Compute the maximum waiting size for each function
3. Average those maxima across all functions in the run

    W_run = mean_i( max_t waiting_size(i, t) )

4. For each workload f (e.g., f10, f15, ...), average W_run
   across repetitions r0..r9

    E[W_max]_f = mean_r( W_run )

This matches the mathematical sketch:
run_id -> function_uuid -> max(waiting_size) -> average -> expectation
"""

import glob
import re

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ==========================================================
# CONFIGURATION
# ==========================================================
RESULTS_GLOB = "results-1-3/f*_r*/application_logs.csv"

PER_RUN_CSV = "waiting_size_per_run_avg_of_max.csv"
SUMMARY_CSV = "waiting_size_summary_avg_of_max.csv"
OUTPUT_PNG = "waiting_size_avg_of_max.png"

# ==========================================================
# FIND ALL FILES
# ==========================================================
files = sorted(glob.glob(RESULTS_GLOB))
print(f"Found {len(files)} performance_samples.csv files")

rows = []

# ==========================================================
# PROCESS EACH RUN
# ==========================================================
for file in files:
    # Extract workload and repetition from directory name
    # Example: results-1-1/f10_r3/performance_samples.csv
    match = re.search(r"f(\d+)_r(\d+)", file)
    if not match:
        continue

    f = int(match.group(1))
    run = int(match.group(2))

    # Read CSV with your standard schema
    try:
        df = pd.read_csv(
            file,
            header=None,
            names=[
                "experiment_name",
                "identifier",
                "target",
                "timestamp",
                "value",
            ],
            on_bad_lines="skip",
        )
    except Exception as e:
        print(f"Could not read {file}: {e}")
        continue

    # Keep only waiting_size telemetry
    df = df[df["target"] == "waiting_size"].copy()

    if df.empty:
        print(f"No waiting_size data in {file}")
        continue

    # Convert values to numeric
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])

    if df.empty:
        continue

    # ======================================================
    # STEP 1: max waiting_size for each function UUID
    # ======================================================
    per_function_max = (
        df.groupby("identifier")["value"]
        .max()
    )

    # ======================================================
    # STEP 2: average of those maxima within this run
    # ======================================================
    run_metric = per_function_max.mean()

    # Optional additional diagnostics
    rows.append(
        {
            "f": f,
            "run": run,
            "num_functions_seen": len(per_function_max),
            "avg_of_function_max": run_metric,
        }
    )

# ==========================================================
# BUILD PER-RUN DATAFRAME
# ==========================================================
results = pd.DataFrame(rows)

if results.empty:
    print("No waiting_size data found.")
    raise SystemExit(1)

results = results.sort_values(["f", "run"]).reset_index(drop=True)

print("\nPer-run metric:")
print(results)

# Save per-run values
results.to_csv(PER_RUN_CSV, index=False)

# ==========================================================
# AGGREGATE ACROSS REPETITIONS
# ==========================================================
summary = (
    results.groupby("f")
    .agg(
        mean=("avg_of_function_max", "mean"),
        std=("avg_of_function_max", "std"),
        n=("avg_of_function_max", "count"),
    )
    .reset_index()
)

# 95% confidence interval
summary["ci95"] = (
    1.96 * summary["std"].fillna(0) / np.sqrt(summary["n"])
)

print("\nFinal summary:")
print(summary)

# Save summary
summary.to_csv(SUMMARY_CSV, index=False)

# ==========================================================
# PLOT
# ==========================================================
plt.figure(figsize=(8, 5))

# Line with markers
plt.plot(
    summary["f"],
    summary["mean"],
    marker="o",
    linewidth=2,
)

# Error bars
plt.errorbar(
    summary["f"],
    summary["mean"],
    yerr=summary["ci95"],
    fmt="none",
    capsize=4,
)

plt.xlabel("Number of Lambda Functions (f)")
plt.ylabel("Average of Per-Function Maximum Waiting Size")
plt.title("Average Maximum Waiting Size per Function")
plt.grid(True)
plt.tight_layout()

plt.savefig(OUTPUT_PNG, dpi=400)
plt.show()

# ==========================================================
# DONE
# ==========================================================
print(f"\nSaved per-run data to: {PER_RUN_CSV}")
print(f"Saved summary to: {SUMMARY_CSV}")
print(f"Saved plot to: {OUTPUT_PNG}")
