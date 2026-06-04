#!/bin/bash

set +e

F_VALUES=(10 20 30 40 50)
REPS=10
DURATION=60

OUTDIR="results-exper"
mkdir -p "$OUTDIR"

echo "============================================"
echo "Starting DISTRIBUTED WORKFLOW experiment"
echo "============================================"

TOTAL=$(( ${#F_VALUES[@]} * REPS ))
COUNT=0

# ----------------------------
# WAIT FOR NODES
# ----------------------------
wait_for_nodes() {
    echo "Waiting for nodes..."
    for i in {1..20}; do
        NODES=$(target/release/proxy_cli show node health 2>/dev/null | wc -l)

        if [ "$NODES" -gt 0 ]; then
            echo "✅ Nodes ready ($NODES)"
            return 0
        fi

        sleep 1
    done

    echo "❌ No nodes detected"
    return 1
}

# ----------------------------
# START WORKFLOW WITH RETRY
# ----------------------------
start_workflow() {
    for attempt in {1..3}; do
        OUTPUT=$(target/release/edgeless_cli workflow start workflow.json 2>/dev/null)
        WF_ID=$(echo "$OUTPUT" | awk '{print $NF}')

        if [[ "$WF_ID" =~ ^[0-9a-f-]+$ ]]; then
            echo "$WF_ID"
            return 0
        fi

        sleep 1
    done

    return 1
}

# ----------------------------
# MAIN LOOP
# ----------------------------
for f in "${F_VALUES[@]}"
do
for ((r=0; r<REPS; r++))
do

COUNT=$((COUNT+1))
echo "--------------------------------------------"
echo "Run $COUNT / $TOTAL"
echo "Workflows=$f | repetition=$r"
echo "--------------------------------------------"

RUN_ID="f${f}_r${r}"
RUN_DIR="${OUTDIR}/${RUN_ID}"
mkdir -p "$RUN_DIR"

# ----------------------------
# CLEAN REDIS
# ----------------------------
echo "Cleaning Redis..."
redis-cli FLUSHALL > /dev/null
sleep 5

# ----------------------------
# CHECK SERVICES
# ----------------------------
nc -z 127.0.0.1 7001 || { echo "❌ Controller down"; exit 1; }
nc -z 127.0.0.1 7000 || { echo "❌ Orchestrator down"; exit 1; }

# ----------------------------
# WAIT FOR NODES
# ----------------------------
wait_for_nodes || continue

# ----------------------------
# START WORKFLOWS
# ----------------------------
WF_IDS=()

for ((i=0; i<f; i++))
do
    WF_ID=$(start_workflow)

    if [ -n "$WF_ID" ]; then
        WF_IDS+=($WF_ID)
    else
        echo "⚠️ Failed workflow $i"
    fi
done

echo "✅ Started ${#WF_IDS[@]} workflows"

if [ ${#WF_IDS[@]} -eq 0 ]; then
    echo "❌ No workflows started — skipping run"
    continue
fi

# ----------------------------
# RUN EXPERIMENT
# ----------------------------
echo "Running workload for ${DURATION}s..."
sleep "$DURATION"

# ----------------------------
# STOP WORKFLOWS
# ----------------------------
echo "Stopping workflows..."
for id in "${WF_IDS[@]}"
do
    target/release/edgeless_cli workflow stop $id 2>/dev/null
done

sleep 5
sleep 10   # telemetry flush

# ----------------------------
# CHECK DATA EXISTS
# ----------------------------
KEYS_COUNT=$(redis-cli KEYS "performance:*" | wc -l)

if [ "$KEYS_COUNT" -eq 0 ]; then
    echo "⚠️ No performance data — skipping run"
    continue
fi

# ----------------------------
# PERFORMANCE CSV
# ----------------------------
echo "Extracting performance..."

PERF_FILE="${RUN_DIR}/performance_${RUN_ID}.csv"
echo "test,identifier,metric,timestamp,value" > "$PERF_FILE"

for key in $(redis-cli KEYS "performance:*"); do
    workflow=$(echo $key | cut -d':' -f2)
    metric=$(echo $key | cut -d':' -f3)

    redis-cli ZRANGE "$key" 0 -1 WITHSCORES | \
    awk -v wf="$workflow" -v m="$metric" -v test="$RUN_ID" \
    'NR%2{val=$0; next}{print test","wf","m","$0","val}' \
    >> "$PERF_FILE"
done

# ----------------------------
# NODE HEALTH CSV
# ----------------------------
echo "Extracting node health..."

HEALTH_FILE="${RUN_DIR}/node_health_${RUN_ID}.csv"
echo "test,timestamp,node_id,metric,value" > "$HEALTH_FILE"

for key in $(redis-cli KEYS "node:health:*"); do
    node=$(echo $key | cut -d':' -f3)

    redis-cli HGETALL "$key" | \
    awk -v n="$node" -v test="$RUN_ID" \
    'NR%2{field=$0; next}{print test","systime()","n","field","$0}' \
    >> "$HEALTH_FILE"
done

# ----------------------------
# DONE
# ----------------------------
echo "✅ Saved results → $RUN_DIR"
echo "✔ Run completed"
sleep 3

done
done

echo "============================================"
echo "All experiments finished"
echo "============================================"
