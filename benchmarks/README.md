# v2 Component Benchmarks

Performance validation suite for the v2 components, measuring against
Gemini-4's specified targets.

## Components

| Benchmark | Target | What it tests |
|-----------|--------|---------------|
| `bench_fastauth.py` | Fast <0.8μs, Slow <100μs | FastAuthEngine decision cache |
| `bench_pool.py` | 238 qps @16 conns | ConnectionPool throughput + idle discard |
| `bench_compress.py` | ~0.28ms/64KB | SmartCompressor skip rules + speed |
| `bench_dedup.py` | <5μs hit, 5000 cap | DedupManager LRU + IN_FLIGHT protection |

## Usage

```bash
cd ~/workspace/muse-vps-deploy/benchmarks
./run_all.sh          # run all, summary table
python3 bench_fastauth.py   # individual
```

## Design

- **Pure stdlib + mocks.** No VPS, no network.
- Each benchmark tries to import the REAL component first
  (e.g. `vps-auth-patch/fast_auth.py`, `mused/pool.py`).
- Falls back to a spec-faithful reference implementation if the
  real component isn't built yet.
- Output header shows which implementation was used.

## Gemini-4 Target Reference

From `~/workspace/user/files/code_artifact__1.md` and `code_artifact__2.md`:

1. **FastAuth**: fast path <0.8μs (blake2b + dict), slow path <100μs
2. **Pool**: 16 conns × (1000ms/67ms) ≈ 238 qps theoretical
3. **Compress**: 0.28ms per 64KB (zlib level=1), 140:1 ROI at 10Mbps
4. **Dedup**: 5000 entries, 256KB single, 64MB total, 300s TTL
