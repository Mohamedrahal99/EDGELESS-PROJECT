import pandas as pd
import matplotlib.pyplot as plt

# read dataset
data = pd.read_csv(
    "dataset/performance_samples.csv",
    header=None,
    names=["experiment","function_id","metric","timestamp","value"]
)

# convert to numeric
data["value"] = pd.to_numeric(data["value"], errors="coerce")

# convert seconds → ms
data["latency_ms"] = data["value"] * 1000

# separate metrics
execution = data[data["metric"] == "function_execution_time"]
transfer = data[data["metric"] == "function_transfer_time"]

# average per function
exec_avg = execution.groupby("function_id")["latency_ms"].mean()
trans_avg = transfer.groupby("function_id")["latency_ms"].mean()

# total latency per function
latency = exec_avg.add(trans_avg, fill_value=0)

# number of 
num_functions = len(latency)
print(num_functions)

avg_latency = latency.mean()
print(avg_latency)

functions = [num_functions]
latencies = [avg_latency]

functions = [0] + functions
latencies = [0] + latencies

plt.figure(figsize=(6,4))

plt.plot(functions, latencies, marker='o')

plt.xlabel("Number of Functions")
plt.ylabel("Latency (ms)")
plt.title("Functions vs Latency")

# start axis from zero
plt.xlim(left=0)
plt.ylim(0, 1.0)

plt.grid(True)

plt.show()