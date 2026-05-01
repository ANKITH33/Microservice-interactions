"""
prometheus_collector.py  (v2)

Extract service-level aggregates from Prometheus instant + range metrics.

IMPORTANT — scope boundary
──────────────────────────
Prometheus does NOT expose endpoint-level data.  grpc_method / grpc_service
remain "unknown" because Envoy cannot parse method-level info from gRPC
traffic.  Therefore this module is used ONLY for service-level aggregation.

All endpoint-level computation (latency percentiles, error rates, call counts,
fan-out) is handled by zipkin_parser.py which uses Zipkin traces as the sole
source of truth.

Metrics extracted here
──────────────────────
Instant (snapshot) — prometheus-metrics.json
  • request_rate     — req/s per service (destination reporter)
  • total_requests   — total request count in window
  • p50/p95/p99      — latency percentiles in ms
  • error_rate       — non-2xx / non-zero gRPC responses/s
  • error_5xx        — 5xx error rate/s
  • retry_rate       — retried request rate/s
  • slo_compliance_500ms — fraction of requests completing within 500ms
  • cpu_cores        — CPU rate (core-seconds/s) per pod → service
  • memory_mb        — memory usage in MB per service
  • call_rate_edges  — per source→destination request rates

Range (time series) — prometheus-metrics-range.json
  • request_rate_ts  — request rate over time
  • p99_latency_ts   — p99 latency over time
  • error_rate_ts    — error rate over time
  • error_5xx_ts     — 5xx rate over time
  • retry_rate_ts    — retry rate over time
  • cpu_usage_ts     — CPU usage over time
  • memory_usage_ts  — memory usage over time (bytes)

Derived metrics
───────────────
  • retry_factor     — total_requests / (total_requests - retried)
                       → amplification from retries
  • slo_compliance_500ms (already a fraction from Prometheus)

Outputs
───────
  outputs/prometheus-processed.json
"""

import json
import os
from collections import defaultdict
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
UPLOADS_DIR = Path("/mnt/user-data/uploads")
OUTPUT_DIR  = Path(__file__).resolve().parent / "outputs"


# ── Helpers ────────────────────────────────────────────────────────────────

def _nan(v: str) -> bool:
    return v in ("NaN", "Inf", "+Inf", "-Inf", "nan", "inf")


def _fval(v: str, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if not (x != x) else default  # NaN check
    except (ValueError, TypeError):
        return default


def _pod_to_service(pod: str) -> str:
    """
    Strip the two trailing hash segments added by ReplicaSet and Pod controllers.
    'frontend-64f465779-6pv92' → 'frontend'
    """
    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod


def _get_or_create(services: dict, svc: str) -> dict:
    if svc not in services:
        services[svc] = {
            "service":            svc,
            "request_rate":       0.0,
            "total_requests":     0.0,
            "error_rate":         0.0,
            "error_5xx_rate":     0.0,
            "retry_rate":         0.0,
            "timeout_rate":       0.0,   # from timeout_rate metric (may be 0)
            "p50_ms":             0.0,
            "p95_ms":             0.0,
            "p99_ms":             0.0,
            "cpu_cores":          0.0,
            "memory_bytes":       0.0,
            "memory_mb":          0.0,
            "slo_compliance_500ms": 0.0,
            "retry_factor":       1.0,   # derived
        }
    return services[svc]


# ── Instant metric extraction ──────────────────────────────────────────────

def extract_service_metrics(prom: dict) -> dict:
    """
    Extract per-service snapshot metrics from prometheus-metrics.json.
    Returns dict keyed by destination_workload (short name, e.g. 'adservice').
    """
    services: dict = {}

    # ── Request rate ────────────────────────────────────────────────────────
    for r in prom.get("request_rate", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["request_rate"] += _fval(r["value"][1])

    # ── Total requests ──────────────────────────────────────────────────────
    for r in prom.get("total_requests", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["total_requests"] += _fval(r["value"][1])

    # ── Error rate (all non-2xx / non-zero gRPC) ────────────────────────────
    for r in prom.get("error_rate", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["error_rate"] += _fval(r["value"][1])

    # ── 4xx error rate ──────────────────────────────────────────────────────
    for r in prom.get("error_4xx", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        # No dedicated field yet; will be computed as error_rate - error_5xx below

    # ── 5xx error rate ──────────────────────────────────────────────────────
    for r in prom.get("error_5xx", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["error_5xx_rate"] += _fval(r["value"][1])

    # ── Retry rate ──────────────────────────────────────────────────────────
    for r in prom.get("retry_rate", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["retry_rate"] += _fval(r["value"][1])

    # ── Timeout rate ────────────────────────────────────────────────────────
    for r in prom.get("timeout_rate", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["timeout_rate"] += _fval(r["value"][1])

    # ── Latency percentiles (ms) ────────────────────────────────────────────
    # Take the max across all label combinations to surface worst observed value.
    for metric_key, field in [
        ("p50_latency", "p50_ms"),
        ("p95_latency", "p95_ms"),
        ("p99_latency", "p99_ms"),
    ]:
        for r in prom.get(metric_key, {}).get("data", {}).get("result", []):
            svc = r["metric"].get("destination_workload", "")
            if not svc:
                continue
            val_str = r["value"][1]
            if _nan(val_str):
                continue
            val   = _fval(val_str)
            entry = _get_or_create(services, svc)
            entry[field] = max(entry[field], val)

    # ── SLO compliance — 500ms target ───────────────────────────────────────
    for r in prom.get("slo_compliance_500ms", {}).get("data", {}).get("result", []):
        svc = r["metric"].get("destination_workload", "")
        if not svc:
            continue
        _get_or_create(services, svc)["slo_compliance_500ms"] = _fval(r["value"][1])

    # ── CPU usage (pod → service) ────────────────────────────────────────────
    cpu_by_svc: dict = defaultdict(float)
    for r in prom.get("cpu_usage", {}).get("data", {}).get("result", []):
        pod = r["metric"].get("pod", "")
        if not pod or _nan(r["value"][1]):
            continue
        svc = _pod_to_service(pod)
        cpu_by_svc[svc] += _fval(r["value"][1])
    for svc, val in cpu_by_svc.items():
        _get_or_create(services, svc)["cpu_cores"] = val

    # ── Memory usage (pod → service, take max) ───────────────────────────────
    for r in prom.get("memory_usage", {}).get("data", {}).get("result", []):
        pod = r["metric"].get("pod", "")
        if not pod or _nan(r["value"][1]):
            continue
        svc   = _pod_to_service(pod)
        entry = _get_or_create(services, svc)
        entry["memory_bytes"] = max(entry["memory_bytes"], _fval(r["value"][1]))

    # ── Derived: retry_factor ────────────────────────────────────────────────
    # retry_factor = total_requests / original_requests
    # original_requests = total_requests - retried_requests
    # retried_requests ≈ retry_rate * window (we use total_requests directly)
    # Simpler: retry_factor = (req_rate + retry_rate) / req_rate
    for svc, e in services.items():
        req_rate   = e["request_rate"]
        retry_rate = e["retry_rate"]
        if req_rate > 0:
            e["retry_factor"] = round(1.0 + retry_rate / req_rate, 4)
        else:
            e["retry_factor"] = 1.0

    # ── Derived: 4xx rate ─────────────────────────────────────────────────────
    for svc, e in services.items():
        e["error_4xx_rate"] = round(
            max(0.0, e["error_rate"] - e["error_5xx_rate"]), 4
        )

    # ── Round and derive MB ───────────────────────────────────────────────────
    for svc in services:
        e = services[svc]
        e["request_rate"]         = round(e["request_rate"],         4)
        e["total_requests"]       = round(e["total_requests"],       2)
        e["error_rate"]           = round(e["error_rate"],           4)
        e["error_5xx_rate"]       = round(e["error_5xx_rate"],       4)
        e["retry_rate"]           = round(e["retry_rate"],           4)
        e["timeout_rate"]         = round(e["timeout_rate"],         4)
        e["p50_ms"]               = round(e["p50_ms"],               3)
        e["p95_ms"]               = round(e["p95_ms"],               3)
        e["p99_ms"]               = round(e["p99_ms"],               3)
        e["cpu_cores"]            = round(e["cpu_cores"],            6)
        e["memory_mb"]            = round(e["memory_bytes"] / 1024 / 1024, 2)
        e["slo_compliance_500ms"] = round(e["slo_compliance_500ms"], 4)

    return services


# ── Per-edge request rates ─────────────────────────────────────────────────

def extract_edge_rates(prom: dict) -> list:
    """
    Extract per-edge (source_workload → destination_workload) request rates.
    Uses call_rate_edges query which has both source_workload and
    destination_workload labels.
    """
    edges: dict = {}

    for r in prom.get("call_rate_edges", {}).get("data", {}).get("result", []):
        m    = r["metric"]
        src  = m.get("source_workload", "unknown")
        dst  = m.get("destination_workload", "")
        if not dst:
            continue
        val = _fval(r["value"][1])
        key = (src, dst)
        edges[key] = edges.get(key, 0.0) + val

    return [
        {
            "source":       src,
            "destination":  dst,
            "request_rate": round(rate, 4),
        }
        for (src, dst), rate in sorted(edges.items(), key=lambda x: -x[1])
    ]


# ── Time series extraction ─────────────────────────────────────────────────

def extract_time_series(prom_range: dict) -> dict:
    """
    Extract time series data from prometheus-metrics-range.json.

    For each metric key, builds a list of:
      { service: str, timestamps: [unix_ts, ...], values: [float, ...] }

    Pod-level metrics (cpu_usage, memory_usage) are mapped to service names
    using _pod_to_service() and summed across pods.

    Returns a dict keyed by metric name.
    """
    POD_METRICS = {"cpu_usage", "memory_usage"}
    ts_out: dict = {}

    for metric_key, data_blob in prom_range.items():
        results = data_blob.get("data", {}).get("result", [])
        if not results:
            ts_out[metric_key] = []
            continue

        # Decide label to use for grouping
        is_pod_metric = metric_key in POD_METRICS

        # Aggregate: map service → {ts → value} (sum for pod metrics, max for others)
        agg: dict = defaultdict(dict)  # svc → {ts: val}

        for r in results:
            m = r["metric"]
            if is_pod_metric:
                pod = m.get("pod", "")
                svc = _pod_to_service(pod) if pod else "unknown"
            else:
                svc = m.get("destination_workload", "unknown")

            for ts_str, val_str in r.get("values", []):
                if _nan(val_str):
                    continue
                ts  = float(ts_str)
                val = _fval(val_str)
                if is_pod_metric:
                    agg[svc][ts] = agg[svc].get(ts, 0.0) + val
                else:
                    agg[svc][ts] = max(agg[svc].get(ts, 0.0), val)

        # Serialise into sorted time series per service
        series_list = []
        for svc, ts_map in sorted(agg.items()):
            ts_sorted = sorted(ts_map.items())
            series_list.append({
                "service":    svc,
                "timestamps": [int(t) for t, _ in ts_sorted],
                "values":     [round(v, 6) for _, v in ts_sorted],
            })

        ts_out[metric_key] = series_list

    return ts_out


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Load instant metrics
    instant_path = UPLOADS_DIR / "prometheus-metrics.json"
    print(f"Loading instant metrics from {instant_path}…")
    with open(instant_path) as f:
        prom = json.load(f)

    print("Extracting service-level metrics…")
    services = extract_service_metrics(prom)
    print(f"  Found {len(services)} services\n")

    header = (f"  {'Service':<35} {'req/s':>7}  {'p50ms':>7}  {'p99ms':>7}  "
              f"{'err/s':>7}  {'5xx/s':>7}  {'retry_f':>8}  {'slo500':>7}  "
              f"{'cpu':>8}  {'mem MB':>8}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for svc, m in sorted(services.items()):
        print(
            f"  {svc:<35} {m['request_rate']:>7.3f}  "
            f"{m['p50_ms']:>7.2f}  {m['p99_ms']:>7.2f}  "
            f"{m['error_rate']:>7.4f}  {m['error_5xx_rate']:>7.4f}  "
            f"{m['retry_factor']:>8.4f}  {m['slo_compliance_500ms']:>7.4f}  "
            f"{m['cpu_cores']:>8.5f}  {m['memory_mb']:>8.1f}"
        )

    print("\nExtracting per-edge request rates…")
    edges = extract_edge_rates(prom)
    print(f"  Found {len(edges)} edges")

    # Load range metrics
    range_path = UPLOADS_DIR / "prometheus-metrics-range.json"
    print(f"\nLoading range metrics from {range_path}…")
    with open(range_path) as f:
        prom_range = json.load(f)

    print("Extracting time series…")
    time_series = extract_time_series(prom_range)
    for k, v in time_series.items():
        print(f"  {k}: {len(v)} service series")

    # Combine into output
    output = {
        "services":      list(services.values()),
        "service_index": services,
        "edges":         edges,
        "time_series":   time_series,
    }

    out_path = OUTPUT_DIR / "prometheus-processed.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved {out_path} ({out_path.stat().st_size:,} bytes)")

    return output


if __name__ == "__main__":
    main()
