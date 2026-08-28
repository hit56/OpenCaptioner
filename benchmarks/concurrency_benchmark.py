import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp


DEFAULT_BASE_URL = os.environ.get("BENCH_BASE_URL", "http://127.0.0.1:7860")
MEDIA_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
}


@dataclass
class CaseResult:
    case_id: int
    ok: bool
    status: str
    total_s: float
    upload_s: Optional[float] = None
    first_event_s: Optional[float] = None
    task_id: Optional[str] = None
    detail: str = ""


def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def summarize_cases(cases: List[CaseResult], wall_s: float) -> Dict[str, Any]:
    ok_cases = [case for case in cases if case.ok]
    total_values = [case.total_s for case in ok_cases]

    return {
        "requests": len(cases),
        "success": len(ok_cases),
        "failed": len(cases) - len(ok_cases),
        "success_rate": round(len(ok_cases) / len(cases), 4) if cases else 0.0,
        "wall_s": round(wall_s, 3),
        "throughput_rps": round(len(ok_cases) / wall_s, 3) if wall_s > 0 else 0.0,
        "latency_p50_s": round(percentile(total_values, 0.50), 3) if total_values else None,
        "latency_p95_s": round(percentile(total_values, 0.95), 3) if total_values else None,
        "latency_mean_s": round(statistics.mean(total_values), 3) if total_values else None,
    }


def format_metric(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def print_summary(title: str, summary: Dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    print(f"requests            : {summary['requests']}")
    print(f"success             : {summary['success']}")
    print(f"failed              : {summary['failed']}")
    print(f"success_rate        : {summary['success_rate']:.2%}")
    print(f"wall_s              : {summary['wall_s']:.3f}")
    print(f"throughput_rps      : {summary['throughput_rps']:.3f}")
    print(f"latency_p50_s       : {format_metric(summary['latency_p50_s'])}")
    print(f"latency_p95_s       : {format_metric(summary['latency_p95_s'])}")
    print(f"latency_mean_s      : {format_metric(summary['latency_mean_s'])}")


def default_media_candidates(root: Path) -> List[Path]:
    candidates = [
        root / "warm_up.wav",
        root / "duix" / "conf" / "hello.mp4",
    ]
    for directory in [root / "saved_data" / "uploads", root / "saved_data" / "cache"]:
        if not directory.is_dir():
            continue
        files = sorted(
            [path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        candidates.extend(files[:10])
    return candidates


def resolve_media_path(path_arg: Optional[str]) -> Path:
    root = Path(__file__).resolve().parents[1]
    if path_arg:
        path = Path(path_arg).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"media file not found: {path}")
        return path

    for candidate in default_media_candidates(root):
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("no benchmark media found; pass --media explicitly")


def load_binary(path: Path) -> bytes:
    return path.read_bytes()


async def collect_sse_until_done(session: aiohttp.ClientSession, url: str, timeout_s: float, start_time: float) -> Dict[str, Any]:
    first_event_s = None
    done_payload = None
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as response:
        response.raise_for_status()
        pending_lines: List[str] = []
        async for raw_line in response.content:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                if not pending_lines:
                    continue
                payload_text = "".join(item[5:].strip() for item in pending_lines if item.startswith("data:"))
                pending_lines = []
                if not payload_text:
                    continue
                payload = json.loads(payload_text)
                if first_event_s is None:
                    first_event_s = time.perf_counter() - start_time
                event_type = payload.get("type")
                if event_type in {"done", "error"}:
                    done_payload = payload
                    break
                continue
            pending_lines.append(line)
    if done_payload is None:
        raise RuntimeError("stream_task ended without done/error event")
    return {
        "first_event_s": first_event_s,
        "done_payload": done_payload,
    }


async def run_offline_case(case_id: int, session: aiohttp.ClientSession, base_url: str, media_name: str, media_bytes: bytes, timeout_s: float) -> CaseResult:
    t0 = time.perf_counter()
    try:
        form = aiohttp.FormData()
        form.add_field("file", media_bytes, filename=media_name, content_type="application/octet-stream")
        async with session.post(f"{base_url}/upload", data=form, timeout=aiohttp.ClientTimeout(total=timeout_s)) as response:
            response.raise_for_status()
            payload = await response.json()
        upload_s = time.perf_counter() - t0
        task_id = payload["task_id"]
        sse_result = await collect_sse_until_done(session, f"{base_url}/stream_task/{task_id}", timeout_s, t0)
        done_payload = sse_result["done_payload"]
        total_s = time.perf_counter() - t0
        ok = done_payload.get("type") == "done"
        return CaseResult(
            case_id=case_id,
            ok=ok,
            status=done_payload.get("type", "unknown"),
            total_s=total_s,
            upload_s=upload_s,
            first_event_s=sse_result["first_event_s"],
            task_id=task_id,
            detail=done_payload.get("message", ""),
        )
    except Exception as exc:
        return CaseResult(case_id=case_id, ok=False, status="exception", total_s=time.perf_counter() - t0, detail=str(exc))


async def run_offline_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    media_path = resolve_media_path(args.media)
    media_bytes = load_binary(media_path)
    log(f"offline benchmark media: {media_path}")

    connector = aiohttp.TCPConnector(limit=max(args.concurrency * 4, 32))
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = asyncio.Semaphore(args.concurrency)
        cases: List[CaseResult] = []

        async def guarded(case_id: int) -> None:
            async with semaphore:
                result = await run_offline_case(case_id, session, args.base_url, media_path.name, media_bytes, args.timeout)
                cases.append(result)
                status = "ok" if result.ok else "fail"
                log(f"offline case={case_id} status={status} total_s={result.total_s:.3f} detail={result.detail[:80]}")

        wall_start = time.perf_counter()
        await asyncio.gather(*(guarded(case_id) for case_id in range(1, args.requests + 1)))
        wall_s = time.perf_counter() - wall_start

    cases.sort(key=lambda item: item.case_id)
    summary = summarize_cases(cases, wall_s)
    return {
        "mode": "offline",
        "label": args.label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "base_url": args.base_url,
            "concurrency": args.concurrency,
            "requests": args.requests,
            "timeout": args.timeout,
            "media": str(media_path),
            "media_bytes": len(media_bytes),
        },
        "summary": summary,
        "cases": [asdict(case) for case in cases],
    }


def save_result(result: Dict[str, Any], output_path: Optional[str]) -> None:
    if not output_path:
        return
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"saved result json: {path}")


def compare_metric(before: Optional[float], after: Optional[float], larger_is_better: bool) -> str:
    if before is None or after is None:
        return "-"
    delta = after - before
    ratio = (after / before) if before not in (0, None) else None
    direction = "better" if (delta > 0 and larger_is_better) or (delta < 0 and not larger_is_better) else "worse"
    if ratio is None:
        return f"delta={delta:.3f} ({direction})"
    return f"delta={delta:.3f}, ratio={ratio:.3f}x ({direction})"


def print_compare(before: Dict[str, Any], after: Dict[str, Any], section_name: str) -> None:
    before_summary = before["summary"]
    after_summary = after["summary"]
    print(f"\n=== Compare: {section_name} ===")
    print(f"throughput_rps : before={format_metric(before_summary.get('throughput_rps'))} after={format_metric(after_summary.get('throughput_rps'))} {compare_metric(before_summary.get('throughput_rps'), after_summary.get('throughput_rps'), True)}")
    print(f"latency_p50_s  : before={format_metric(before_summary.get('latency_p50_s'))} after={format_metric(after_summary.get('latency_p50_s'))} {compare_metric(before_summary.get('latency_p50_s'), after_summary.get('latency_p50_s'), False)}")
    print(f"latency_p95_s  : before={format_metric(before_summary.get('latency_p95_s'))} after={format_metric(after_summary.get('latency_p95_s'))} {compare_metric(before_summary.get('latency_p95_s'), after_summary.get('latency_p95_s'), False)}")
    print(f"success_rate   : before={before_summary.get('success_rate', 0.0):.2%} after={after_summary.get('success_rate', 0.0):.2%} {compare_metric(before_summary.get('success_rate'), after_summary.get('success_rate'), True)}")


def run_compare(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))

    if before.get("mode") == "suite" and after.get("mode") == "suite":
        if "offline" in before and "offline" in after:
            print_compare(before["offline"], after["offline"], "offline")
        return 0

    if before.get("mode") != after.get("mode"):
        print("mode mismatch between baseline and candidate", file=sys.stderr)
        return 2
    print_compare(before, after, before.get("mode", "unknown"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Concurrency benchmark for gateway offline upload path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--label", default="run", help="result label")
    common.add_argument("--media", default=None, help="media file for benchmark")
    common.add_argument("--base-url", default=DEFAULT_BASE_URL, help="gateway base url")
    common.add_argument("--concurrency", type=int, default=4, help="max concurrent in-flight sessions")
    common.add_argument("--requests", type=int, default=12, help="total benchmark requests")
    common.add_argument("--timeout", type=float, default=300.0, help="per request timeout in seconds")
    common.add_argument("--save", default=None, help="write result json to this path")

    offline_parser = subparsers.add_parser("offline", parents=[common], help="benchmark upload + async offline processing")
    offline_parser.set_defaults(handler="offline")

    compare_parser = subparsers.add_parser("compare", help="compare two saved benchmark json files")
    compare_parser.add_argument("before", help="baseline result json")
    compare_parser.add_argument("after", help="candidate result json")
    compare_parser.set_defaults(handler="compare")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.handler == "compare":
        return run_compare(args)

    if args.handler == "offline":
        result = asyncio.run(run_offline_benchmark(args))
        print_summary("Offline Benchmark", result["summary"])
        save_result(result, args.save)
        return 0 if result["summary"]["failed"] == 0 else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
