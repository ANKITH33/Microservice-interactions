"""
prometheus_collector.py

Reads saved Prometheus instant-vector metrics and extracts per-service
request rate, latency percentiles (p50/p95/p99), error rate, CPU, memory.

Design notes:
  - Uses reporter=destination to avoid double-counting (source and destination
    sidecars both emit metrics for the same request; destination is authoritative).
  - Latency values from Istio histograms are already in milliseconds.
  - CPU is a rate (cores/s); memory is raw bytes at the snapshot time.
  - Pod names are mapped to service names by stripping the two trailing
    replicaset/pod hash suffixes (e.g. frontend-64f465779-6pv92 → frontend).
  - Per-edge request rates are extracted for use by the graph builders
    so that endpoint bottleneck scores use actual observed rates rather
    than trace-window estimates.

Input:  prometheus-metrics.json (from uploads or project directory)
Output: outputs/prometheus-processed.json
"""

import json
import os

from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent

BASE_DIR = CURRENT_DIR.parent / "baseline" / "outputs-baseline"
OUTPUT_DIR = CURRENT_DIR / "outputs"


# ── helpers ────────────────────────────────────────────────────────────────

def _nan(val_str: str) -> bool:
    return val_str in ("NaN", "Inf", "+Inf", "-Inf")


def _pod_to_service(pod: str) -> str:
    """
    Strip the two trailing hash segments added by ReplicaSet and Pod controllers.
    e.g. 'frontend-64f465779-6pv92'      → 'frontend'
         'productcatalogservice-59456f9d9-rwhfv' → 'productcatalogservice'
    """
    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod


def _get_or_create(services: dict, svc: str) -> dict:
    if svc not in services:
        services[svc] = {
            "service":       svc,
            "request_rate":  0.0,   # req/s (sum across all callers)
            "error_rate":    0.0,   # err/s (non-2xx/non-0-grpc responses)
            "p50_ms":        0.0,
            "p95_ms":        0.0,
            "p99_ms":        0.0,
            "cpu_cores":     0.0,   # rate of CPU core-seconds
            "memory_bytes":  0.0,
            "memory_mb":     0.0,
        }
    return services[svc]


# ── main extraction ────────────────────────────────────────────────────────

def extract_service_metrics(prom: dict) -> dict:
    """
    Returns dict keyed by service name (as it appears in Istio labels,
    e.g. 'adservice', 'frontend-external').
    """
    services: dict = {}

    # ── Request rate ───────────────────────────────────────────────────────
    # Sum all source→dest rates at the destination reporter side.
    for result in prom.get("request_rate", {}).get("data", {}).get("result", []):
        m = result["metric"]
        if m.get("reporter") != "destination":
            continue
        svc = m.get("destination_service_name", "")
        if not svc:
            continue
        val = float(result["value"][1])
        _get_or_create(services, svc)["request_rate"] += val

    # ── Error rate ─────────────────────────────────────────────────────────
    # error_rate query already filters to non-2xx / non-zero-grpc responses.
    for result in prom.get("error_rate", {}).get("data", {}).get("result", []):
        m = result["metric"]
        if m.get("reporter") != "destination":
            continue
        svc = m.get("destination_service_name", "")
        if not svc:
            continue
        val = float(result["value"][1])
        _get_or_create(services, svc)["error_rate"] += val

    # ── Latency percentiles ────────────────────────────────────────────────
    # Take the maximum across all label combinations for a given service so
    # the worst observed latency is surfaced (conservative / safe choice).
    # Istio histogram buckets give values in milliseconds.
    for metric_key, field in [
        ("p50_latency", "p50_ms"),
        ("p95_latency", "p95_ms"),
        ("p99_latency", "p99_ms"),
    ]:
        for result in prom.get(metric_key, {}).get("data", {}).get("result", []):
            m = result["metric"]
            if m.get("reporter") != "destination":
                continue
            svc = m.get("destination_service_name", "")
            if not svc:
                continue
            val_str = result["value"][1]
            if _nan(val_str):
                continue
            val = float(val_str)   # already in ms
            entry = _get_or_create(services, svc)
            entry[field] = max(entry[field], val)

    # ── CPU usage ─────────────────────────────────────────────────────────
    # cpu_usage is a rate (core-seconds/second) per pod from cAdvisor.
    # We sum across all pods that map to the same service name.
    for result in prom.get("cpu_usage", {}).get("data", {}).get("result", []):
        m = result["metric"]
        pod = m.get("pod", "")
        if not pod:
            continue
        val_str = result["value"][1]
        if _nan(val_str):
            continue
        svc = _pod_to_service(pod)
        _get_or_create(services, svc)["cpu_cores"] += float(val_str)

    # ── Memory usage ──────────────────────────────────────────────────────
    # Take the max across pods (usually one pod per service in this cluster).
    for result in prom.get("memory_usage", {}).get("data", {}).get("result", []):
        m = result["metric"]
        pod = m.get("pod", "")
        if not pod:
            continue
        val_str = result["value"][1]
        if _nan(val_str):
            continue
        svc = _pod_to_service(pod)
        entry = _get_or_create(services, svc)
        entry["memory_bytes"] = max(entry["memory_bytes"], float(val_str))

    # ── Round and derive MB ────────────────────────────────────────────────
    for svc in services:
        e = services[svc]
        e["request_rate"] = round(e["request_rate"], 4)
        e["error_rate"]   = round(e["error_rate"],   4)
        e["p50_ms"]       = round(e["p50_ms"],        3)
        e["p95_ms"]       = round(e["p95_ms"],        3)
        e["p99_ms"]       = round(e["p99_ms"],        3)
        e["cpu_cores"]    = round(e["cpu_cores"],     6)
        e["memory_mb"]    = round(e["memory_bytes"] / 1024 / 1024, 2)

    return services


def extract_edge_rates(prom: dict) -> list:
    """
    Extract per-edge (source_app → destination_service_name) request rates.
    These are used by the metrics calculator to assign accurate per-endpoint
    request rates instead of estimating from trace call counts.

    Returns list of dicts: {source, destination, request_rate, response_code}
    """
    edges: dict = {}

    for result in prom.get("request_rate", {}).get("data", {}).get("result", []):
        m = result["metric"]
        if m.get("reporter") != "destination":
            continue
        src  = m.get("source_app", "unknown")
        dst  = m.get("destination_service_name", "")
        code = m.get("response_code", "200")
        if not dst:
            continue
        val = float(result["value"][1])
        key = (src, dst, code)
        edges[key] = edges.get(key, 0.0) + val

    return [
        {
            "source":        src,
            "destination":   dst,
            "response_code": code,
            "request_rate":  round(rate, 4),
        }
        for (src, dst, code), rate in edges.items()
    ]


def _find_input_file() -> str:
    candidates = [
        "/mnt/user-data/uploads/prometheus-metrics.json",
        os.path.join(BASE_DIR, "prometheus-metrics.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "prometheus-metrics.json not found. Checked:\n" + "\n".join(candidates)
    )


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    input_file = _find_input_file()
    print(f"Loading Prometheus metrics from {input_file}...")
    with open(input_file) as f:
        prom = json.load(f)

    print("Extracting per-service metrics...")
    services = extract_service_metrics(prom)
    print(f"  Found metrics for {len(services)} services\n")

    print(f"  {'Service':<35} {'req/s':>7}  {'p50ms':>7}  {'p99ms':>7}  {'err/s':>7}  {'cpu':>8}  {'mem MB':>8}")
    print(f"  {'-'*35} {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*8}")
    for svc, m in sorted(services.items()):
        print(
            f"  {svc:<35} {m['request_rate']:>7.3f}  "
            f"{m['p50_ms']:>7.2f}  {m['p99_ms']:>7.2f}  "
            f"{m['error_rate']:>7.4f}  {m['cpu_cores']:>8.5f}  {m['memory_mb']:>8.1f}"
        )

    print("\nExtracting per-edge request rates...")
    edges = extract_edge_rates(prom)
    print(f"  Found {len(edges)} edges")

    output = {
        "services":      list(services.values()),
        "service_index": services,
        "edges":         edges,
    }

    output_file = os.path.join(OUTPUT_DIR, "prometheus-processed.json")
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved {output_file} ({os.path.getsize(output_file):,} bytes)")


if __name__ == "__main__":
    main()