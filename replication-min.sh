#!/bin/bash

F_VALUES=(1 2 4 6)
REPS=10
DURATION=60

echo "============================================"
echo "Simple Workflow Experiment"
echo "============================================"

for f in "${F_VALUES[@]}"
do
for ((r=0; r<REPS; r++))
do

echo "--------------------------------------------"
echo "Workflows=$f | repetition=$r"
echo "--------------------------------------------"

WF_IDS=()

# start workflows
for ((i=0; i<f; i++))
do
    WF_ID=$(target/release/edgeless_cli workflow start workflow-min.json | awk '{print $NF}')
    if [ -n "$WF_ID" ]; then
        WF_IDS+=($WF_ID)
    else
        echo "Failed to start workflow $i"
    fi
done

echo "f=$f r=$r started=${#WF_IDS[@]}"

done

# show running functions (actual deployment view)
echo "Current deployed functions:"
target/release/proxy_cli show functions

# run
echo "Running for ${DURATION}s..."
sleep "$DURATION"

# stop workflows
echo "Stopping workflows..."
for id in "${WF_IDS[@]}"
do
    target/release/edgeless_cli workflow stop $id
done

sleep 5

echo "Remaining functions after stop:"
target/release/proxy_cli show functions

done
done

echo "============================================"
echo "Done"
echo "============================================"
