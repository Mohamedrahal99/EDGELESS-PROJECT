#!/bin/bash

set +e

F_VALUES=(10 20 30 40 50)
P_VALUES=(0 0.5 1)

REPS=10
DURATION=30

OUTDIR="results"
LOGDIR="logs"
METRIC_FILE="dataset/performance_samples.csv"

mkdir -p "$OUTDIR"
mkdir -p "$LOGDIR"

echo "Starting experiment batch"

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
LOGFILE="${LOGDIR}/${RUN_ID}.log"
CLILOG="${LOGDIR}/${RUN_ID}_cli.log"

rm -f "$METRIC_FILE"

# generate workflow
python3 generate-workflow.py $f >> "$LOGFILE" 2>&1

# start infrastructure with FULL logs
RUST_LOG=debug target/release/edgeless_inabox > "$LOGFILE" 2>&1 &
EDGELESS_PID=$!

sleep 5

ps -p $EDGELESS_PID > /dev/null
if [ $? -ne 0 ]; then
    echo "Infrastructure failed to start" | tee -a "$LOGFILE"
    continue
fi

# start workflow (capture CLI output)
OUTPUT=$(target/release/edgeless_cli workflow start workflow.json 2>&1 | tee "$CLILOG")

WF_ID=$(echo "$OUTPUT" | grep -oE '[0-9a-fA-F-]{36}')

if [ -z "$WF_ID" ]; then
    echo "Workflow failed to start" | tee -a "$LOGFILE"
    kill $EDGELESS_PID 2>/dev/null
    wait $EDGELESS_PID 2>/dev/null
    continue
fi

echo "Workflow started: $WF_ID" | tee -a "$LOGFILE"

# send traffic (log failures if any)
for i in {1..50}; do
    curl -s http://127.0.0.1:7007 >> "$LOGFILE" 2>&1 &
done

sleep "$DURATION"

# stop workflow
target/release/edgeless_cli workflow stop $WF_ID >> "$CLILOG" 2>&1

sleep 2

# save metrics
if [ -f "$METRIC_FILE" ]; then
    mv "$METRIC_FILE" "$OUTFILE"
    echo "Saved results -> $OUTFILE" | tee -a "$LOGFILE"
else
    echo "Warning: metrics file missing" | tee -a "$LOGFILE"
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
