import pandas as pd
import matplotlib.pyplot as plt

data = pd.read_csv(
    "dataset/performance_samples.csv",
    header=None,
    names=["experiment","function_id","metric","timestamp","value"]
)

data["value"] = pd.to_numeric(data["value"], errors="coerce")

data["latency_ms"] = data["value"] * 1000

execution = data[data["metric"] == "function_execution_time"]
transfer = data[data["metric"] == "function_transfer_time"]

exec_avg = execution.groupby("function_id")["latency_ms"].mean()
trans_avg = transfer.groupby("function_id")["latency_ms"].mean()

exec_latency = exec_avg.values
transfer_latency = trans_avg.values


transfer_ratio = transfer_latency / (exec_latency + transfer_latency)

num_functions = len(exec_latency)


functions = list(range(1, num_functions + 1))


functions = [0] + functions
transfer_ratio = [0] + list(transfer_ratio)

plt.figure(figsize=(6,4))

plt.plot(functions, transfer_ratio, marker='o')

plt.xlabel("Number of Functions")
plt.ylabel("Transfer Ratio")
plt.title("Communication Overhead")

plt.xlim(left=0)
plt.ylim(0,1)

plt.grid(True)

plt.show()