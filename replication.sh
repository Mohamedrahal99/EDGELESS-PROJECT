
#!/bin/bash

set +e

F_VALUES=(10 20 30 40 50)

REPS=10
DURATION=30

OUTDIR="results-0.5"

METRIC_FILE="dataset/performance_samples.csv"
CPU_FILE="dataset/health_status.csv"
APP_LOG_FILE="dataset/application_logs.csv"

mkdir -p "$OUTDIR"

echo "Starting experiment batch"

# clean leftover processes
pkill edgeless_inabox 2>/dev/null
sleep 5

TOTAL=$(( ${#F_VALUES[@]} * REPS ))
COUNT=0

for f in "${F_VALUES[@]}"
do
for ((r=0; r<REPS; r++))
do

COUNT=$((COUNT+1))
echo "--------------------------------------------"
echo "Run $COUNT / $TOTAL"
echo "Running experiment: f=$f r=$r"
echo "--------------------------------------------"

RUN_ID="f${f}_r${r}"

OUTFILE="${OUTDIR}/${RUN_ID}.csv"
CPU_OUT="${OUTDIR}/${RUN_ID}_cpu.csv"
APP_OUT="${OUTDIR}/${RUN_ID}_app.csv"

# =========================
# CLEAN PREVIOUS DATA
# =========================
rm -f "$METRIC_FILE"
rm -f "$CPU_FILE"
rm -f "$APP_LOG_FILE"

# generate workflow
python3 generate-workflow.py $f

# =========================
# START INFRASTRUCTURE
# =========================
RUST_LOG=info cargo run --release --bin edgeless_inabox &
EDGELESS_PID=$!

sleep 5   # allow full startup

# verify infrastructure started
ps -p $EDGELESS_PID > /dev/null
if [ $? -ne 0 ]; then
    echo "Infrastructure failed to start for f=$f r=$r"
    continue
fi

# =========================
# START WORKFLOW
# =========================
WF_ID=$(target/debug/edgeless_cli workflow start workflow.json | awk '{print $NF}')

if [ -z "$WF_ID" ]; then
    echo "Workflow failed to start for f=$f r=$r"
    kill $EDGELESS_PID 2>/dev/null
    wait $EDGELESS_PID 2>/dev/null
    continue
fi

echo "Workflow started: $WF_ID"

# =========================
# RUN EXPERIMENT
# =========================
sleep "$DURATION"

# =========================
# STOP WORKFLOW
# =========================
target/debug/edgeless_cli workflow stop $WF_ID 2>/dev/null

sleep 5   # allow logs to flush

# =========================
# SAVE OUTPUT FILES
# =========================

# performance metrics
if [ -f "$METRIC_FILE" ]; then
    mv "$METRIC_FILE" "$OUTFILE"
    echo "Saved metrics -> $OUTFILE"
else
    echo "Warning: metrics file missing for f=$f r=$r"
fi

# CPU logs
if [ -f "$CPU_FILE" ]; then
    mv "$CPU_FILE" "$CPU_OUT"
    echo "Saved CPU -> $CPU_OUT"
else
    echo "Warning: CPU file missing for f=$f r=$r"
fi

# application logs (transaction latency)
if [ -f "$APP_LOG_FILE" ]; then
    mv "$APP_LOG_FILE" "$APP_OUT"
    echo "Saved app logs -> $APP_OUT"
else
    echo "Warning: app logs missing for f=$f r=$r"
fi

# =========================
# STOP INFRASTRUCTURE
# =========================
kill $EDGELESS_PID 2>/dev/null
wait $EDGELESS_PID 2>/dev/null

sleep 5

done
done

echo "============================================"
echo "All experiments finished"
echo "============================================"
