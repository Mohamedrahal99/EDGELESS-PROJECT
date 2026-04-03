import pandas as pd
import matplotlib.pyplot as plt

file = "dataset/performance_samples.csv"

data = pd.read_csv(
    file,
    header=None,
    names=["experiment","function_id","metric","timestamp","value"]
)

data["value"] = pd.to_numeric(data["value"], errors="coerce")

data["latency_ms"] = data["value"] * 1000

execution = data[data["metric"] == "function_execution_time"]
transfer = data[data["metric"] == "function_transfer_time"]

exec_avg = execution.groupby("function_id")["latency_ms"].mean()
trans_avg = transfer.groupby("function_id")["latency_ms"].mean()

total_latency = exec_avg.add(trans_avg, fill_value=0)

latencies = total_latency.values

functions = list(range(1, len(latencies) + 1))

plt.figure(figsize=(6,4))

plt.scatter(functions, latencies)
plt.plot(functions, latencies)

plt.xlabel("Function")
plt.ylabel("Latency (ms)")
plt.title("Lambda Latency per Function")

plt.grid(True)

plt.show()