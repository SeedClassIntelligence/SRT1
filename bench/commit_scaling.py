"""Commit latency vs stream size. Measures where whole-stream loading becomes a problem.

Usage: python3 bench/commit_scaling.py [max_items] [threshold_ms]
Prints median / p95 commit time (ms) per size bucket and the first bucket whose median crosses the threshold.

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from srt1 import SRT1  # noqa: E402


def main(max_items=3000, threshold_ms=100.0, step=250):
    s = SRT1(os.path.join(os.environ.get("TMPDIR", "/tmp"), f"srt1_bench_{os.getpid()}.db"))
    sid = s.create_stream("bench")
    s.observe("INTENT: benchmark stream", sid)
    times, crossed = [], None
    print(f"{'items':>7} {'median_ms':>10} {'p95_ms':>9}")
    for i in range(1, max_items + 1):
        t0 = time.perf_counter()
        r = s.observe(f"DISCOVERY: finding {i} about index design and query plans", sid)
        times.append((time.perf_counter() - t0) * 1000)
        assert r.status == "COMMITTED", r
        if i % step == 0:
            w = times[-step:]
            med, p95 = statistics.median(w), sorted(w)[int(len(w) * 0.95) - 1]
            print(f"{i:>7} {med:>10.1f} {p95:>9.1f}", flush=True)
            if crossed is None and med > threshold_ms:
                crossed = i
                break
    print(f"threshold {threshold_ms:.0f} ms median crossed at: {crossed if crossed else '> ' + str(max_items)} items")
    os.remove(s.store.path)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3000, float(sys.argv[2]) if len(sys.argv) > 2 else 100.0)
