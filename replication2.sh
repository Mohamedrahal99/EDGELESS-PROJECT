#!/bin/bash

set +e   # continue even if a run fails

F_VALUES=(10 20 30 40 50)
P_VALUES=(0 0.5 1)

REPS=10
DURATION=30

OUTDIR="res"
METRIC_FILE="dataset/performance_samples.csv"

mkdir -p "$OUTDIR"

echo "Starting experiment batch"

# clean leftover processes
pkill edgeless_inabox 2>/dev/null
sleep 2

TOTAL=$(( ${#F_VALUES[@]} * ${#P_VALUES[@]} * REPS ))
COUNT=0

for f in "${F_VALUES[@]}"
do
for p in "${P_VALUES[@]}"
do
for ((r=0; r<REPS; r++))
do

COUNT=$((COUNT+1))
echo "--------------------------------------------"
echo "Run $COUNT / $TOTAL"
echo "Running experiment: f=$f p=$p r=$r"
echo "--------------------------------------------"

RUN_ID="f${f}_p${p}_r${r}"
OUTFILE="${OUTDIR}/${RUN_ID}.csv"

rm -f "$METRIC_FILE"

# generate workflow
python3 generate-workflow.py $f

# start infrastructure
RUST_LOG=info target/release/edgeless_inabox > /dev/null 2>&1 &
EDGELESS_PID=$!

# 🔴 FIX 1: give system time to be ready
sleep 5

# verify infrastructure started
ps -p $EDGELESS_PID > /dev/null
if [ $? -ne 0 ]; then
    echo "Infrastructure failed to start"
    continue
fi

# start workflow
OUTPUT=$(target/release/edgeless_cli workflow start workflow.json)

# 🔴 FIX 2: extract only valid UUID
WF_ID=$(echo "$OUTPUT" | grep -oE '[0-9a-fA-F-]{36}')

if [ -z "$WF_ID" ]; then
    echo "Workflow failed to start"
    kill $EDGELESS_PID 2>/dev/null
    wait $EDGELESS_PID 2>/dev/null
    continue
fi

echo "Workflow started: $WF_ID"

# 🔴 FIX 3: send light traffic so workflow actually runs
for i in {1..50}; do
    curl -s http://127.0.0.1:7007 > /dev/null &
done

# run experiment
sleep "$DURATION"

# stop workflow
target/release/edgeless_cli workflow stop $WF_ID 2>/dev/null

sleep 2

# save metrics
if [ -f "$METRIC_FILE" ]; then
    mv "$METRIC_FILE" "$OUTFILE"
    echo "Saved results -> $OUTFILE"
else
    echo "Warning: metrics file missing"
fi

# stop infrastructure
kill $EDGELESS_PID 2>/dev/null
wait $EDGELESS_PID 2>/dev/null

sleep 2

done
done
done

echo "============================================"
echo "All experiments finished"
echo "============================================"