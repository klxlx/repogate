"""RepoGate 本地缓存命中压测：只测报告读取路径，不打 GitHub API 配额。

用法（服务需已启动）：
    python scripts/loadtest.py --total 100 --concurrency 20
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

DEFAULT_REPO = "https://github.com/octocat/Hello-World"


async def run(base: str, repo: str, total: int, concurrency: int) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    failures: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as client:

        async def one() -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.get(f"{base}/api/inspect", params={"repo": repo})
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.status_code != 200:
                        failures.append(f"HTTP {response.status_code}")
                        return
                    latencies.append(elapsed)
                except Exception as exc:  # noqa: BLE001 - 压测脚本记录任意失败
                    failures.append(type(exc).__name__)

        began = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(total)))
        wall = time.perf_counter() - began

    if not latencies:
        print(f"全部失败：{failures[:5]}")
        return 1

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    p99 = latencies[max(0, int(len(latencies) * 0.99) - 1)]
    print(f"总请求 {total} · 并发 {concurrency} · 成功 {len(latencies)} · 失败 {len(failures)}")
    print(f"wall {wall * 1000:.0f}ms · rps {len(latencies) / wall:.1f}")
    print(f"p50 {p50:.0f}ms · p95 {p95:.0f}ms · p99 {p99:.0f}ms · max {latencies[-1]:.0f}ms")
    if failures:
        print(f"失败样例：{failures[:5]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    return asyncio.run(run(args.base, args.repo, args.total, args.concurrency))


if __name__ == "__main__":
    raise SystemExit(main())
