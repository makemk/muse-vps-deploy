#!/usr/bin/env bash
# run_all.sh: run the full v2 benchmark suite, print summary table.
# Pure stdlib + mocks. No VPS, no network.
set -u
cd "$(dirname "$0")"

PASS=0
FAIL=0
declare -a ROWS

run_one() {
    local name="$1" script="$2"
    echo ""
    echo "################################################################"
    echo "# $name"
    echo "################################################################"
    if python3 "$script"; then
        PASS=$((PASS+1))
        ROWS+=("$name | PASS")
    else
        FAIL=$((FAIL+1))
        ROWS+=("$name | FAIL")
    fi
}

run_one "FastAuthEngine (fast <0.8us, slow <100us)" bench_fastauth.py
run_one "ConnectionPool (idle-discard, throughput)" bench_pool.py
run_one "SmartCompressor (0.28ms/64KB, skip rules)" bench_compress.py
run_one "DedupManager (LRU, IN_FLIGHT protection)" bench_dedup.py

echo ""
echo "================================================================"
echo "BENCHMARK SUMMARY vs Gemini-4 targets"
echo "================================================================"
printf "%-45s %s\n" "Component" "Result"
echo "----------------------------------------------------------------"
for r in "${ROWS[@]}"; do
    printf "%-45s %s\n" "${r%% | *}" "${r##*| }"
done
echo "================================================================"
echo "Passed: $PASS  Failed: $FAIL"
if [ "$FAIL" -eq 0 ]; then
    echo "OVERALL: ALL PASS"
    exit 0
else
    echo "OVERALL: $FAIL FAILURES"
    exit 1
fi
