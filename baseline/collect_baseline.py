#!/usr/bin/env python3
"""
Baseline data collector for Online Boutique on Minikube + Istio
Dumps Zipkin traces and Prometheus metrics to outputs-baseline/

Usage:
    python3 collect_baseline.py

Requirements:
    pip3 install requests --break-system-packages

Prereqs:
    - kubectl port-forward svc/zipkin 9411:9411 -n istio-system
    - kubectl port-forward svc/prometheus 9090:9090 -n istio-system
"""

import json
import os
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip3 install requests --break-system-packages")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
ZIPKIN_URL      = "http://localhost:9411"
PROMETHEUS_URL  = "http://localhost:9090"
OUTPUT_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs-baseline")
TRACE_LIMIT     = 1000
LOOKBACK_MS     = 10 * 60 * 1000   # 10 minutes
LOOKBACK_SEC    = 10 * 60           # 10 minutes

PROMETHEUS_QUERIES = {
    "request_rate":     'rate(istio_requests_total[5m])',
    "p50_latency":      'histogram_quantile(0.50, rate(istio_request_duration_milliseconds_bucket[5m]))',
    "p95_latency":      'histogram_quantile(0.95, rate(istio_request_duration_milliseconds_bucket[5m]))',
    "p99_latency":      'histogram_quantile(0.99, rate(istio_request_duration_milliseconds_bucket[5m]))',
    "error_rate":       'rate(istio_requests_total{response_code!~"2.."}[5m])',
    "cpu_usage":        'rate(container_cpu_usage_seconds_total{namespace="default"}[5m])',
    "memory_usage":     'container_memory_usage_bytes{namespace="default"}',
    "request_duration": 'istio_request_duration_milliseconds_bucket',
    "total_requests":   'istio_requests_total',
}
# ─────────────────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def save(data, filename):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    size = os.path.getsize(path)
    log(f"  Saved {filename} ({size:,} bytes)")
    return path

def check_connectivity():
    log("Checking connectivity...")
    for name, url in [
        ("Zipkin",      f"{ZIPKIN_URL}/api/v2/services"),
        ("Prometheus",  f"{PROMETHEUS_URL}/api/v1/query?query=up"),
    ]:
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            log(f"  {name}: OK")
        except Exception as e:
            log(f"  ERROR: Cannot reach {name} at {url}: {e}")
            log(f"  Make sure port-forward is running.")
            sys.exit(1)

def collect_zipkin_services():
    log("Fetching Zipkin service list...")
    r = requests.get(f"{ZIPKIN_URL}/api/v2/services", timeout=10)
    r.raise_for_status()
    services = r.json()
    log(f"  Found {len(services)} services: {services}")
    save(services, "zipkin-services.json")
    return services

def collect_zipkin_traces(services):
    log("Fetching Zipkin traces (last 10 minutes)...")
    all_traces = []
    end_ts = int(time.time() * 1000)

    for svc in services:
        log(f"  Fetching traces for {svc}...")
        try:
            r = requests.get(
                f"{ZIPKIN_URL}/api/v2/traces",
                params={
                    "serviceName": svc,
                    "limit": TRACE_LIMIT,
                    "endTs": end_ts,
                    "lookback": LOOKBACK_MS,
                },
                timeout=30,
            )
            r.raise_for_status()
            traces = r.json()
            log(f"    Got {len(traces)} traces")
            all_traces.extend(traces)
        except Exception as e:
            log(f"    WARNING: Failed to fetch traces for {svc}: {e}")

    # Deduplicate by traceId
    seen = set()
    unique_traces = []
    for trace in all_traces:
        if trace:
            tid = trace[0].get("traceId")
            if tid and tid not in seen:
                seen.add(tid)
                unique_traces.append(trace)

    log(f"  Total unique traces: {len(unique_traces)}")
    save(unique_traces, "zipkin-traces.json")
    return unique_traces

def collect_zipkin_dependencies():
    log("Fetching Zipkin dependency graph...")
    end_ts = int(time.time() * 1000)
    try:
        r = requests.get(
            f"{ZIPKIN_URL}/api/v2/dependencies",
            params={"endTs": end_ts, "lookback": LOOKBACK_MS},
            timeout=10,
        )
        r.raise_for_status()
        deps = r.json()
        log(f"  Found {len(deps)} dependency links")
        save(deps, "zipkin-dependencies.json")
        return deps
    except Exception as e:
        log(f"  WARNING: Failed to fetch dependencies: {e}")
        return []

def collect_prometheus():
    log("Fetching Prometheus point-in-time metrics...")
    results = {}
    for name, query in PROMETHEUS_QUERIES.items():
        log(f"  Querying: {name}...")
        try:
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            results[name] = data
            count = len(data.get("data", {}).get("result", []))
            log(f"    Got {count} result series")
        except Exception as e:
            log(f"    WARNING: Failed to query {name}: {e}")
            results[name] = {"error": str(e)}

    save(results, "prometheus-metrics.json")
    return results

def collect_prometheus_range():
    log("Fetching Prometheus range metrics (last 10 minutes)...")
    end = int(time.time())
    start = end - LOOKBACK_SEC
    step = "15s"

    range_queries = {
        "request_rate": 'rate(istio_requests_total[1m])',
        "p99_latency":  'histogram_quantile(0.99, rate(istio_request_duration_milliseconds_bucket[1m]))',
        "error_rate":   'rate(istio_requests_total{response_code!~"2.."}[1m])',
        "cpu_usage":    'rate(container_cpu_usage_seconds_total{namespace="default"}[1m])',
        "memory_usage": 'container_memory_usage_bytes{namespace="default"}',
    }

    results = {}
    for name, query in range_queries.items():
        log(f"  Range query: {name}...")
        try:
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            results[name] = data
            count = len(data.get("data", {}).get("result", []))
            log(f"    Got {count} time series")
        except Exception as e:
            log(f"    WARNING: Failed range query {name}: {e}")
            results[name] = {"error": str(e)}

    save(results, "prometheus-metrics-range.json")
    return results

def build_summary(services, traces, deps, prom):
    log("Building summary...")
    summary = {
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "lookback_minutes": LOOKBACK_MS // 60000,
        "zipkin": {
            "services_found": len(services),
            "services": services,
            "total_traces": len(traces),
            "dependency_links": len(deps),
            "dependencies": deps,
        },
        "prometheus": {
            "queries_run": len(prom),
            "queries": list(prom.keys()),
        },
        "files": [
            "zipkin-services.json",
            "zipkin-traces.json",
            "zipkin-dependencies.json",
            "prometheus-metrics.json",
            "prometheus-metrics-range.json",
            "collection-summary.json",
        ]
    }
    save(summary, "collection-summary.json")
    return summary

def main():
    log("=" * 60)
    log("Baseline Data Collector — Online Boutique")
    log("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log(f"Output directory: {OUTPUT_DIR}")

    check_connectivity()

    services    = collect_zipkin_services()
    traces      = collect_zipkin_traces(services)
    deps        = collect_zipkin_dependencies()
    prom        = collect_prometheus()
    prom_range  = collect_prometheus_range()
    summary     = build_summary(services, traces, deps, prom)

    log("=" * 60)
    log("Collection complete.")
    log(f"  Services:    {summary['zipkin']['services_found']}")
    log(f"  Traces:      {summary['zipkin']['total_traces']}")
    log(f"  Dep links:   {summary['zipkin']['dependency_links']}")
    log(f"  Output dir:  {OUTPUT_DIR}")
    log("=" * 60)

if __name__ == "__main__":
    main()