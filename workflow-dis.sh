#!/bin/bash

set +e

F_VALUES=(10 20 30 40 50)
REPS=10
DURATION=60

OUTDIR="results-exper"

DATASET_DIR="dataset"

mkdir -p "$OUTDIR"

echo "============================================"
echo "Starting DISTRIBUTED WORKFLOW experiment"
echo "============================================"

TOTAL=$(( ${#F_VALUES[@]} * REPS ))
COUNT=0

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

# =========================
# CLEAN DATASET (fresh run)
# =========================
echo "Cleaning dataset..."
rm -f ${DATASET_DIR}/*.csv ${DATASET_DIR}/redis.log 2>/dev/null

# =========================
# CHECK INFRASTRUCTURE
# =========================
echo "Checking controller..."
nc -z 127.0.0.1 7001 || { echo "❌ Controller not running"; exit 1; }

echo "Checking orchestrator..."
nc -z 127.0.0.1 7000 || { echo "❌ Orchestrator not running"; exit 1; }

# =========================
# START WORKFLOWS
# =========================
WF_IDS=()

for ((i=0; i<f; i++))
do
    WF_ID=$(target/release/edgeless_cli workflow start workflow.json | awk '{print $NF}')

    if [ -n "$WF_ID" ]; then
        WF_IDS+=($WF_ID)
    else
        echo "⚠️ Failed to start workflow $i"
    fi
done

echo "✅ Started ${#WF_IDS[@]} workflows"

# =========================
# RUN EXPERIMENT
# =========================
echo "Running workload for ${DURATION}s..."
sleep "$DURATION"

# =========================
# STOP WORKFLOWS
# =========================
echo "Stopping workflows..."
for id in "${WF_IDS[@]}"
do
    target/release/edgeless_cli workflow stop $id 2>/dev/null
done

sleep 5

# =========================
# WAIT FOR TELEMETRY FLUSH
# =========================
echo "Waiting for telemetry flush..."
sleep 10

sync
sleep 3

# =========================
# SAVE RESULTS (COPY, NOT MOVE)
# =========================
echo "Saving results..."

cp ${DATASET_DIR}/*.csv "$RUN_DIR/" 2>/dev/null
cp ${DATASET_DIR}/redis.log "$RUN_DIR/" 2>/dev/null

# quick validation
FILE_COUNT=$(ls "$RUN_DIR" | wc -l)

if [ "$FILE_COUNT" -eq 0 ]; then
    echo "⚠️ WARNING: No files copied for this run"
else
    echo "✅ Saved $FILE_COUNT files in $RUN_DIR"
fi

echo "✔ Run completed"
sleep 5

done
done

echo "============================================"
echo "All experiments finished"
echo "============================================"
