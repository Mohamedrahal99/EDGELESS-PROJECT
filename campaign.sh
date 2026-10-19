#!/bin/bash
# SPDX-License-Identifier: MIT
#
# campaign.sh — the baseline characterisation campaign described in
# docs/experiment_plan.md.
#
#   cd /home/EDGELESS-PROJECT
#   ./campaign.sh calibration    
#   ./campaign.sh load           
#
# Every run records, in $OUT:
#   <run>_rounds.csv      one row per round, with the agent class
#   <run>_summary.json    makespan and latency stats per session
#   <run>_nodes.csv       node telemetry sampled every 5 s (gpu, power, mem, load)
#   <run>_instances.txt   proxy_cli show node instances, taken while running
#   <run>_fids.txt        controller journal: fid -> workflow, component
#   <run>_placement.txt   orchestrator placement log (RandomNode only)

set -u
cd /home/EDGELESS-PROJECT

POLICIES="${POLICIES:-WeightedLeastLoaded}"
REPS="${REPS:-5}"
HORIZON="${HORIZON:-60}"
COMPRESSION="${COMPRESSION:-1200}"
DATA="${DATA:-/home/EDGELESS-PROJECT/data_dpm}"
WORKFLOW="${WORKFLOW:-functions/convmem/workflow.json}"
OUT="${OUT:-results/campaign}"
SETTLE="${SETTLE:-60}"
ANSWER_HOST="${ANSWER_HOST:-172.20.20.5}" 

mkdir -p "$OUT"

# --------------------------------------------------------------------------
switch_policy () {
    local policy="$1"
    echo
    echo "############## $policy ##############"
    sed -i "s/orchestration_strategy = \".*\"/orchestration_strategy = \"$policy\"/" \
        orchestrator.toml
    systemctl restart edgeless-orc.service
    sleep "$SETTLE"

    local active nprov
    active=$(journalctl -u edgeless-orc.service --since "2 min ago" --no-pager \
             | grep -i "Orchestration logic strategy" | tail -1 | sed 's/.*strategy: //')
    nprov=$(target/release/proxy_cli show node resource-providers 2>/dev/null \
            | grep -ci ollama)
    echo "  strategy in force : ${active:-UNKNOWN}"
    echo "  ollama providers  : $nprov"
    if [ "$nprov" -lt 1 ]; then
        echo "  !! no ollama provider registered - skipping $policy"
        return 1
    fi
    return 0
}

# run_cell <run_id> <sessions> <elephant_frac>
run_cell () {
    local run="$1" sessions="$2" frac="$3"

    if [ -f "$OUT/${run}_summary.json" ]; then
        echo "  = $run already done, skipping"
        return 0
    fi
    echo "  ---------- $run (N=$sessions, elephants=$frac) ----------"

    target/release/edgeless_cli workflow stop all >/dev/null 2>&1
    pkill -f "proxy_cli show node health" 2>/dev/null
    sleep 2
    date +%s > /tmp/run_start

    # node telemetry, every 5 s. proxy_cli prints "<host> -> memory free ..."
    echo "unix_ts,node,gpu_load_perc,active_power_mw,mem_used_kb,load1_perc" \
        > "$OUT/${run}_nodes.csv"
    ( while true; do
        TS=$(date +%s)
        target/release/proxy_cli show node health 2>/dev/null \
          | awk -v ts="$TS" '
              /->/ {
                node=$1; gpu=""; pw=""; mem=""; l1="";
                if (match($0, /gpu_load_perc [0-9-]+/)) gpu=substr($0,RSTART+14,RLENGTH-14);
                if (match($0, /active_power [0-9-]+/))  pw=substr($0,RSTART+13,RLENGTH-13);
                if (match($0, /used [0-9]+ kb/))        mem=substr($0,RSTART+5,RLENGTH-8);
                if (match($0, /1 minute [0-9]+%/))      l1=substr($0,RSTART+9,RLENGTH-10);
                printf "%s,%s,%s,%s,%s,%s\n", ts, node, gpu, pw, mem, l1;
              }'
        sleep 5
      done >> "$OUT/${run}_nodes.csv" ) &
    local sampler=$!

    # Placement, captured while the workflows are live.
    ( sleep 25
      target/release/proxy_cli show node instances > "$OUT/${run}_instances.txt" 2>/dev/null
      journalctl -u edgeless-con.service --since "@$(cat /tmp/run_start)" --no-pager \
        | grep "started with fid" > "$OUT/${run}_fids.txt" 2>/dev/null ) &

    python3 functions/convmem/driver.py \
        --usecase pdm --data-dir "$DATA" \
        --sessions "$sessions" --machine-id 1 \
        --agent-classes --alternate-classes --elephant-frac "$frac" \
        --horizon-hours "$HORIZON" --time-compression "$COMPRESSION" \
        --answer-timeout 180 --answer-host "$ANSWER_HOST" \
        --on-timeout retry --max-retries 2 \
        --quiet-answers --no-save-answers \
        --workflow "$WORKFLOW" \
        --out-dir "$OUT" --run-id "$run"

    kill "$sampler" 2>/dev/null
    pkill -f "proxy_cli show node health" 2>/dev/null

    journalctl -u edgeless-orc.service --since "@$(cat /tmp/run_start)" --no-pager \
        | grep "Placement:" > "$OUT/${run}_placement.txt" 2>/dev/null

    echo "    telemetry=$(( $(wc -l < "$OUT/${run}_nodes.csv") - 1 )) samples" \
         "instances=$(grep -c '\[' "$OUT/${run}_instances.txt" 2>/dev/null || echo 0)" \
         "fids=$(grep -c . "$OUT/${run}_fids.txt" 2>/dev/null || echo 0)"

    target/release/edgeless_cli workflow stop all >/dev/null 2>&1
    sleep 5
}

# --------------------------------------------------------------------------
# Calibration: one agent at a time, so there is no contention at all. Gives the
# per-class cost that the mouse-equivalent model depends on.
calibration () {
    echo "=== CALIBRATION: isolated single agents, no contention ==="
    switch_policy "Intelligent" || return
    for CLS in mouse elephant; do
        FRAC=$([ "$CLS" = elephant ] && echo 1.0 || echo 0.0)
        for REP in $(seq 1 5); do
            run_cell "calib_${CLS}_r${REP}" 1 "$FRAC"
        done
    done
}

# Sweep A: offered load. 50/50 composition throughout.
load_sweep () {
    echo "=== SWEEP A: offered load, 50% elephants ==="
    for POLICY in $POLICIES; do
        switch_policy "$POLICY" || continue
        for N in 35; do
            for REP in $(seq 1 "$REPS"); do
                run_cell "load_${POLICY}_N${N}_r${REP}" "$N" 0.5
            done
        done
    done
}

composition_sweep () {
    echo "=== SWEEP B: composition at N=10 ==="
    for POLICY in $POLICIES; do
        switch_policy "$POLICY" || continue
        for FRAC in 0.2 0.8; do
            for REP in $(seq 1 "$REPS"); do
                run_cell "comp_${POLICY}_f${FRAC}_r${REP}" 10 "$FRAC"
            done
        done
    done
}

case "${1:-all}" in
    calibration)  calibration ;;
    load)         load_sweep ;;
    composition)  composition_sweep ;;
    all)          calibration; load_sweep; composition_sweep ;;
    *)            echo "usage: $0 {calibration|load|composition|all}"; exit 1 ;;
esac
