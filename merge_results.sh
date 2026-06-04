#!/bin/bash

set -e  # stop if any error

echo "Creating results directory..."

echo "Copying p0 files..."
for f in results_p0-2/*.csv; do
  cp "$f" "results2/p0_$(basename "$f")"
done

echo "Copying p0.5 files..."
for f in results_p0.5-2/*.csv; do
  cp "$f" "results2/p05_$(basename "$f")"
done

echo "Copying p1 files..."
for f in results_p1-2/*.csv; do
  cp "$f" "results2/p1_$(basename "$f")"
done

echo "Done ✅ All files copied to ./results"
