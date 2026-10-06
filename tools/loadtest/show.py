"""Print the headline numbers from one or more harness.py --out files."""
import json
import sys

KEYS = ["students", "users", "limits", "answers", "answers_without_verdict",
        "commands_met_with_silence", "exhausted", "wall_s", "updates", "updates_per_s",
        "cpu_util", "loop_lag_p99_ms", "loop_lag_max_ms", "rss_peak_mb",
        "db_calls_per_update", "db_time_avg_ms", "db_slowest_ms", "db_pool_saturated_pct",
        "api_max_inflight", "flood_429_global", "flood_429_chat", "peak_sends_per_s",
        "forbidden", "guard_retries"]

for path in sys.argv[1:]:
    with open(path) as fh:
        r = json.load(fh)
    print(f"\n######## {path}")
    print({k: r[k] for k in KEYS if k in r})
    for k, v in r["latency_s"].items():
        print(f"  {k:18} n={v['n']:5} p50={v['p50']:6} p95={v['p95']:6} "
              f"p99={v['p99']:6} max={v['max']}")
    for k in ("errors_logged", "raw_exceptions", "bad_requests"):
        if r.get(k):
            print(f" {k}:", r[k])
    print(" integrity:", r["integrity"])
    for k in ("phases", "delivery", "live_student_latency_during_push"):
        if k in r:
            print(f" {k}:", r[k])
