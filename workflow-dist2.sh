#!/bin/bash

# ==========================================================
# CONFIGURATION
# ==========================================================

# Number of lambda functions inside the generated workflow
F_VALUES=(1)

# Number of repetitions per workload
REPS=10

# Duration of each run in seconds
DURATION=60

# Output directories
OUTDIR="results-lamb"
DATASET_DIR="dataset"

mkdir -p "$OUTDIR"

# Stop immediately if a command fails
set -e

# ==========================================================
# MAIN LOOP
# ==========================================================

for f in "${F_VALUES[@]}"
do
    for ((r=0; r<REPS; r++))
    do
        RUN_ID="f${f}_r${r}"
        RUN_DIR="${OUTDIR}/${RUN_ID}"
        mkdir -p "$RUN_DIR"

        echo "============================================"
        echo "Running workload: f=$f, run=$r"
        echo "Output directory: $RUN_DIR"
        echo "============================================"

        # ==================================================
        # GENERATE WORKFLOW WITH PYTHON SCRIPT
        # ==================================================
        echo "Generating workflow with $f lambda functions..."

        python3 generate-workflow.py "$f"

        if [ ! -f workflow.json ]; then
            echo "❌ workflow.json was not generated"
            exit 1
        fi

        # ==================================================
        # EMPTY DATASET CSV FILES (KEEP FILES, REMOVE CONTENT)
        # ==================================================
        echo "Clearing dataset CSV files..."

        for file in "${DATASET_DIR}"/*.csv
        do
            [ -f "$file" ] && truncate -s 0 "$file"
        done

        # ==================================================
        # START THE GENERATED WORKFLOW ONCE
        # (workflow.json already contains f lambda functions)
        # ==================================================
        echo "Starting workflow..."

        WF_ID=$(
            target/release/edgeless_cli workflow start workflow.json \
            | awk '{print $NF}'
        )

        if [[ ! "$WF_ID" =~ ^[0-9a-f-]+$ ]]; then
            echo "❌ Failed to start workflow"
            exit 1
        fi

        echo "Started workflow ID: $WF_ID"

        # ==================================================
        # RUN EXPERIMENT
        # ==================================================
        echo "Running for $DURATION seconds..."
        sleep "$DURATION"

        # ==================================================
        # STOP WORKFLOW
        # ==================================================
        echo "Stopping workflow..."
        target/release/edgeless_cli workflow stop "$WF_ID" \
            > /dev/null 2>&1 || true

        # Allow telemetry/logs to flush
        echo "Waiting 10 seconds for data flush..."
        sleep 10

        # ==================================================
        # COPY DATASET FILES TO RUN DIRECTORY
        # ==================================================
        echo "Copying dataset files..."
        cp "${DATASET_DIR}"/*.csv "$RUN_DIR/" 2>/dev/null || true

        # Also save the exact workflow used for this run
        cp workflow.json "$RUN_DIR/" 2>/dev/null || true

        # ==================================================
        # VALIDATION
        # ==================================================
        COUNT=$(find "$RUN_DIR" -maxdepth 1 -type f | wc -l)

        if [ "$COUNT" -eq 0 ]; then
            echo "⚠️ No files were copied to $RUN_DIR"
        else
            echo "✅ Saved $COUNT files in $RUN_DIR"
        fi

        echo ""
    done
done

# ==========================================================
# FINISHED
# ==========================================================

echo "============================================"
echo "All experiments completed successfully."
echo "Results stored in: $OUTDIR"
echo "============================================"
