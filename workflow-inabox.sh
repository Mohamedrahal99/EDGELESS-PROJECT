#!/bin/bash

F_VALUES=(20 25 30 35 40)
REPS=10
DURATION=60

OUTDIR="results-inabox"
DATASET_DIR="dataset"

mkdir -p "$OUTDIR"

echo "============================================"
echo "INABOX EXPERIMENT"
echo "============================================"

for f in "${F_VALUES[@]}"
do
for ((r=0; r<REPS; r++))
do

RUN_ID="f${f}_r${r}"
RUN_DIR="${OUTDIR}/${RUN_ID}"
mkdir -p "$RUN_DIR"

echo "--------------------------------------------"
echo "Running f=$f r=$r"
echo "--------------------------------------------"

# =========================
# GENERATE WORKFLOW
# =========================
python3 generate-workflow.py $f

# =========================
# CLEAN DATASET CONTENT
# =========================
for file in ${DATASET_DIR}/*.csv
do
    [ -f "$file" ] && : > "$file"
done

# =========================
# START INABOX
# =========================
RUST_LOG=info ./target/release/edgeless_inabox > /dev/null 2>&1 &
EDGELESS_PID=$!

sleep 7

# verify it started
ps -p $EDGELESS_PID > /dev/null
if [ $? -ne 0 ]; then
    echo "❌ Inabox failed to start"
    continue
fi

WF_IDS=()

# =========================
# START WORKFLOWS
# =========================
for ((i=0; i<f; i++))
do
    OUTPUT=$(./target/release/edgeless_cli workflow start workflow-min.json)

    WF_ID=$(echo "$OUTPUT" | grep -oE '[0-9a-fA-F-]{36}')

    if [ -n "$WF_ID" ]; then
        WF_IDS+=($WF_ID)
    else
        echo "⚠️ Workflow $i failed"
    fi
done

echo "started=${#WF_IDS[@]}"

# =========================
# RUN
# =========================
sleep "$DURATION"

# =========================
# STOP WORKFLOWS
# =========================
for id in "${WF_IDS[@]}"
do
    ./target/release/edgeless_cli workflow stop "$id" > /dev/null 2>&1
done

# wait telemetry flush
sleep 10

# =========================
# STOP INABOX
# =========================
kill $EDGELESS_PID 2>/dev/null
wait $EDGELESS_PID 2>/dev/null

# =========================
# COPY DATASET
# =========================
cp ${DATASET_DIR}/*.csv "$RUN_DIR/" 2>/dev/null

COUNT=$(ls "$RUN_DIR" 2>/dev/null | wc -l)
if [ "$COUNT" -eq 0 ]; then
    echo "⚠️ No files copied"
else
    echo "✅ Saved $COUNT files in $RUN_DIR"
fi

echo ""

done
done

echo "============================================"
echo "Done"
echo "============================================"
