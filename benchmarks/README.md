# Concurrency Benchmark

Unified benchmark entry: [concurrency_benchmark.py](concurrency_benchmark.py).

## Modes

- **offline**: upload + SSE task completion through `/upload` and `/stream_task/{task_id}`

## Quick start

```bash
python benchmarks/concurrency_benchmark.py offline --concurrency 6 --requests 18 --save benchmarks/results/offline_before.json
python benchmarks/concurrency_benchmark.py offline --concurrency 6 --requests 18 --save benchmarks/results/offline_after.json
python benchmarks/concurrency_benchmark.py compare benchmarks/results/offline_before.json benchmarks/results/offline_after.json
```

## 3-round median runs

```bash
bash benchmarks/run_benchmark_3rounds.sh offline --label before -- --media warm_up.wav --concurrency 6 --requests 18
bash benchmarks/run_benchmark_3rounds.sh offline --label after -- --media warm_up.wav --concurrency 6 --requests 18
```

## Notes

- Default gateway base URL: `http://127.0.0.1:7860` (`BENCH_BASE_URL`)
- Use a representative media file via `--media`
