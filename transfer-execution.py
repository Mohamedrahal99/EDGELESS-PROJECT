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


exec_latency = exec_avg.mean()
transfer_latency = trans_avg.mean()


num_functions = len(exec_avg)

print("Functions:", num_functions)
print("Execution latency:", exec_latency)
print("Transfer latency:", transfer_latency)

functions = [0, num_functions]


exec_values = [0, exec_latency]
transfer_values = [0, transfer_latency]


plt.figure(figsize=(6,4))

plt.plot(functions, exec_values, marker='o', label="execution")
plt.plot(functions, transfer_values, marker='x', label="transfer")

plt.legend()
plt.xlabel("Number of Functions")
plt.ylabel("Latency (ms)")
plt.title("Transfer vs Execution Latency")

plt.xlim(left=0)
plt.ylim(0, 1.0)

plt.grid(True)

plt.show()