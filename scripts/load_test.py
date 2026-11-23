"""只读 API 并发测试。凭证通过 HUB_LOAD_TOKEN 环境变量传入。"""
import argparse
import asyncio
import json
import math
import os
import statistics
import time
from pathlib import Path

import httpx


async def run(base_url, credential, concurrency, rounds):
    timings = []
    endpoint_timings = {path: [] for path in ('/api/menus', '/api/pages', '/api/workspace')}
    errors = []
    status_counts = {}
    async with httpx.AsyncClient(base_url=base_url, headers={'Authorization': 'Bearer ' + credential},
                                limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
                                timeout=60, trust_env=False) as client:
        warm = await client.get('/api/menus')
        warm.raise_for_status()
        started = time.perf_counter()
        for _ in range(rounds):
            barrier = asyncio.Barrier(concurrency)

            async def request(index):
                await barrier.wait()
                before = time.perf_counter()
                path = ['/api/menus', '/api/pages', '/api/workspace'][index % 3]
                try:
                    response = await client.get(path)
                    status_counts[str(response.status_code)] = status_counts.get(str(response.status_code), 0) + 1
                    if response.status_code != 200:
                        errors.append(str(response.status_code))
                except httpx.HTTPError as exc:
                    errors.append(type(exc).__name__)
                elapsed = (time.perf_counter() - before) * 1000
                timings.append(elapsed)
                endpoint_timings[path].append(elapsed)

            await asyncio.gather(*(request(i) for i in range(concurrency)))
        duration = time.perf_counter() - started
    def latency_summary(samples):
        ordered = sorted(samples)
        return {'mean_ms': round(statistics.mean(samples), 2),
                'p50_ms': round(ordered[math.ceil(len(ordered) * .50) - 1], 2),
                'p95_ms': round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
                'p99_ms': round(ordered[math.ceil(len(ordered) * .99) - 1], 2),
                'max_ms': round(ordered[-1], 2)}

    return {'concurrent_clients': concurrency, 'rounds': rounds, 'requests': len(timings),
            'duration_seconds': round(duration, 3), 'requests_per_second': round(len(timings) / duration, 2),
            'latency': latency_summary(timings),
            'endpoints': {path: latency_summary(samples) for path, samples in endpoint_timings.items()},
            'status_counts': status_counts, 'errors': len(errors), 'error_types': sorted(set(errors)),
            'scope': '同一租户只读 API，本机短时并发测试；包含 MCP 凭证鉴权及 last_used_at 更新，不代表持续压力、上传或生产容量验收'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--concurrency', type=int, default=1000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--output', default='.runtime/load-test.json')
    args = parser.parse_args()
    credential = os.environ.get('HUB_LOAD_TOKEN')
    if not credential:
        parser.error('请通过 HUB_LOAD_TOKEN 环境变量传入只读 MCP 凭证')
    if not 1 <= args.concurrency <= 10000 or not 1 <= args.rounds <= 100:
        parser.error('concurrency 范围 1–10000；rounds 范围 1–100')
    result = asyncio.run(run(args.base_url, credential, args.concurrency, args.rounds))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
