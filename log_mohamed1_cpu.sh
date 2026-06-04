#!/bin/bash

echo "timestamp,cpu_usage" > mohamed1_cpu.csv

while true; do
    cpu=$(top -bn1 | grep "Cpu(s)" | awk '{print 100 - $8}')
    echo "$(date +%s),$cpu" >> mohamed1_cpu.csv
    sleep 1
done
