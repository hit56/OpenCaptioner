#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
BENCH_PY="$SCRIPT_DIR/concurrency_benchmark.py"
PYTHON_BIN=${PYTHON_BIN:-python}
RUNS=${RUNS:-3}
PAUSE_SECONDS=${PAUSE_SECONDS:-5}
RESULT_ROOT_DEFAULT="$SCRIPT_DIR/results"

usage() {
    cat <<'EOF'
Usage:
  bash benchmarks/run_benchmark_3rounds.sh offline [options] [-- extra benchmark args]

Options:
  --label NAME          Label prefix for this batch. Default: auto timestamp
  --result-root DIR     Directory for output JSON files. Default: benchmarks/results
  --runs N              Number of rounds. Default: 3
  --pause SECONDS       Pause between rounds. Default: 5
  --python BIN          Python executable. Default: python or $PYTHON_BIN
  -h, --help            Show this help

Examples:
  bash benchmarks/run_benchmark_3rounds.sh offline --label before -- --media warm_up.wav --concurrency 6 --requests 18
  bash benchmarks/run_benchmark_3rounds.sh offline --label after -- --media warm_up.wav --concurrency 6 --requests 18
EOF
}

if [[ $# -lt 1 ]]; then
    usage
    exit 2
fi

MODE=$1
shift

case "$MODE" in
    offline)
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        echo "Unsupported mode: $MODE" >&2
        usage
        exit 2
        ;;
esac

LABEL=""
RESULT_ROOT="$RESULT_ROOT_DEFAULT"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --label)
            LABEL=${2:-}
            shift 2
            ;;
        --result-root)
            RESULT_ROOT=${2:-}
            shift 2
            ;;
        --runs)
            RUNS=${2:-}
            shift 2
            ;;
        --pause)
            PAUSE_SECONDS=${2:-}
            shift 2
            ;;
        --python)
            PYTHON_BIN=${2:-}
            shift 2
            ;;
        --)
            shift
            EXTRA_ARGS=("$@")
            break
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$LABEL" ]]; then
    LABEL="${MODE}_$(date +%Y%m%d_%H%M%S)"
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR="$RESULT_ROOT/${LABEL}_${TIMESTAMP}"
mkdir -p "$OUT_DIR"

cd "$REPO_ROOT"

echo "[Runner] mode=$MODE runs=$RUNS out_dir=$OUT_DIR"
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    echo "[Runner] extra args: ${EXTRA_ARGS[*]}"
fi

ROUND_FILES=()
for round in $(seq 1 "$RUNS"); do
    ROUND_LABEL="${LABEL}_round${round}"
    ROUND_JSON="$OUT_DIR/round_${round}.json"
    ROUND_LOG="$OUT_DIR/round_${round}.log"
    ROUND_FILES+=("$ROUND_JSON")

    echo "[Runner] starting round ${round}/${RUNS}"
    set +e
    "$PYTHON_BIN" "$BENCH_PY" "$MODE" --label "$ROUND_LABEL" --save "$ROUND_JSON" "${EXTRA_ARGS[@]}" | tee "$ROUND_LOG"
    EXIT_CODE=${PIPESTATUS[0]}
    set -e
    echo "$EXIT_CODE" > "$OUT_DIR/round_${round}.exit_code"

    if [[ $EXIT_CODE -ne 0 ]]; then
        echo "[Runner] round ${round} exited with code $EXIT_CODE" >&2
    else
        echo "[Runner] round ${round} completed"
    fi

    if [[ $round -lt $RUNS ]]; then
        echo "[Runner] sleeping ${PAUSE_SECONDS}s before next round"
        sleep "$PAUSE_SECONDS"
    fi
done

MEDIAN_JSON="$OUT_DIR/median_summary.json"
SUMMARY_TXT="$OUT_DIR/median_summary.txt"
RUN_FILES_JOINED=$(printf '%s\n' "${ROUND_FILES[@]}")
export RUN_FILES_JOINED MEDIAN_JSON SUMMARY_TXT MODE LABEL OUT_DIR RUNS

"$PYTHON_BIN" - <<'PY'
import json
import os
import statistics
from pathlib import Path


def median_value(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(statistics.median(values), 3)


def collect_summary_payload(payload):
    return payload.get("summary", {})


def aggregate_result(result):
    metrics = [
        "requests",
        "success",
        "failed",
        "success_rate",
        "wall_s",
        "throughput_rps",
        "latency_p50_s",
        "latency_p95_s",
        "latency_mean_s",
        "first_partial_p50_s",
        "first_partial_p95_s",
        "first_final_p50_s",
        "first_final_p95_s",
        "stop_complete_p50_s",
        "stop_complete_p95_s",
        "reprocess_done_p50_s",
        "reprocess_done_p95_s",
    ]
    summary_list = [collect_summary_payload(item) for item in result]
    return {metric: median_value([summary.get(metric) for summary in summary_list]) for metric in metrics}


def build_report(paths):
    items = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths if Path(path).exists()]
    if not items:
        raise SystemExit("no round result json files found")

    mode = items[0].get("mode")
    report = {
        "mode": mode,
        "label": os.environ["LABEL"],
        "runs": len(items),
        "out_dir": os.environ["OUT_DIR"],
        "round_files": paths,
    }

    if mode == "suite" and "offline" in items[0]:
        report["offline"] = {
            "median_summary": aggregate_result([item["offline"] for item in items]),
            "round_labels": [item["offline"].get("label") for item in items],
        }
    else:
        report["median_summary"] = aggregate_result(items)
        report["round_labels"] = [item.get("label") for item in items]

    return report


def lines_for_summary(title, summary):
    keys = [
        "requests",
        "success",
        "failed",
        "success_rate",
        "wall_s",
        "throughput_rps",
        "latency_p50_s",
        "latency_p95_s",
        "latency_mean_s",
        "first_partial_p50_s",
        "first_partial_p95_s",
        "first_final_p50_s",
        "first_final_p95_s",
        "stop_complete_p50_s",
        "stop_complete_p95_s",
        "reprocess_done_p50_s",
        "reprocess_done_p95_s",
    ]
    lines = [f"=== {title} ==="]
    for key in keys:
        value = summary.get(key)
        if value is None:
            continue
        if isinstance(value, float):
            lines.append(f"{key:22}: {value:.3f}")
        else:
            lines.append(f"{key:22}: {value}")
    return lines


paths = [line for line in os.environ["RUN_FILES_JOINED"].splitlines() if line.strip()]
report = build_report(paths)
Path(os.environ["MEDIAN_JSON"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

text_lines = [
    f"mode                  : {report['mode']}",
    f"label                 : {report['label']}",
    f"runs                  : {report['runs']}",
    f"out_dir               : {report['out_dir']}",
]
if report["mode"] == "suite" and "offline" in report:
    text_lines.append("")
    text_lines.extend(lines_for_summary("offline median summary", report["offline"]["median_summary"]))
else:
    text_lines.append("")
    text_lines.extend(lines_for_summary("median summary", report["median_summary"]))

summary_text = "\n".join(text_lines) + "\n"
Path(os.environ["SUMMARY_TXT"]).write_text(summary_text, encoding="utf-8")
print(summary_text, end="")
print(f"median_json: {os.environ['MEDIAN_JSON']}")
print(f"median_txt : {os.environ['SUMMARY_TXT']}")
PY

echo "[Runner] all done"