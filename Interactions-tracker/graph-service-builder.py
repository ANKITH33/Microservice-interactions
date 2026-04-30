"""
graph_service_builder.py

Builds a weighted service-level dependency graph from parsed spans.

Design notes:
  - Nodes  = services (one per unique serviceName in traces)
  - Edges  = (caller_svc, callee_svc) pairs with aggregated weights
  - MIN_EDGE_CALLS = 10: edges with fewer calls are statistically unreliable
    and excluded.  A single observed call tells us nothing about steady-state
    behaviour; 10 calls gives a reasonable latency distribution.
  - AIS (Afferent Instability Score): how many distinct services call this one.
    High AIS = wide blast radius if this service degrades.
  - ADS (Afferent Dependency Score): how many distinct services this one calls.
  - ACS = AIS * ADS (Absolute Coupling Score)
  - SDP = ADS / (AIS + ADS)  (Stable Dependency Proportion; 0 = maximally stable)

Input:  outputs/parsed-spans.json
Output: outputs/graph-service.json
"""

import json
import os
import statistics


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_DIR   = os.path.join(os.path.dirname(CURRENT_DIR), "baseline", "outputs-baseline")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
INPUT_FILE = os.path.join(OUTPUT_DIR, "parsed-spans.json")

# Statistical significance threshold: minimum calls on an edge to include it.
# Rationale: 10 calls in a 10-minute window ≈ 1 call/min — enough to estimate
# a latency distribution.  Below this the p99 estimate is meaningless.
MIN_EDGE_CALLS = 10


def percentile(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return s[idx]


def build_graph(spans: list) -> dict:

    # ── Per-service aggregation ────────────────────────────────────────────
    svc_durations: dict = {}
    svc_errors:    dict = {}
    svc_calls:     dict = {}

    for s in spans:
        svc = s["service"]
        svc_durations.setdefault(svc, []).append(s["duration_ms"])
        svc_errors.setdefault(svc, 0)
        svc_calls.setdefault(svc, 0)
        svc_calls[svc]  += 1
        if s["is_error"]:
            svc_errors[svc] = svc_errors.get(svc, 0) + 1

    # ── Build nodes ────────────────────────────────────────────────────────
    nodes: dict = {}
    for svc, durs in svc_durations.items():
        nodes[svc] = {
            "service":     svc,
            "call_count":  svc_calls[svc],
            "error_count": svc_errors.get(svc, 0),
            "error_rate":  round(svc_errors.get(svc, 0) / max(svc_calls[svc], 1), 4),
            "latency": {
                "p50_ms":  round(percentile(durs, 50), 3),
                "p95_ms":  round(percentile(durs, 95), 3),
                "p99_ms":  round(percentile(durs, 99), 3),
                "mean_ms": round(statistics.mean(durs), 3),
                "max_ms":  round(max(durs), 3),
            },
        }

    # ── Build edges ────────────────────────────────────────────────────────
    edge_data: dict = {}
    for s in spans:
        if not s.get("caller_svc"):
            continue
        key = (s["caller_svc"], s["service"])
        if key not in edge_data:
            edge_data[key] = {"durations": [], "errors": 0}
        edge_data[key]["durations"].append(s["duration_ms"])
        if s["is_error"]:
            edge_data[key]["errors"] += 1

    edges = []
    for (caller, callee), data in edge_data.items():
        durs = data["durations"]
        # Enforce minimum-call threshold for statistical reliability
        if len(durs) < MIN_EDGE_CALLS:
            continue
        edges.append({
            "caller":      caller,
            "callee":      callee,
            "call_count":  len(durs),
            "error_count": data["errors"],
            "error_rate":  round(data["errors"] / max(len(durs), 1), 4),
            "latency": {
                "p50_ms":  round(percentile(durs, 50), 3),
                "p95_ms":  round(percentile(durs, 95), 3),
                "p99_ms":  round(percentile(durs, 99), 3),
                "mean_ms": round(statistics.mean(durs), 3),
                "max_ms":  round(max(durs), 3),
            },
        })

    # ── Coupling metrics per service ───────────────────────────────────────
    # AIS: count of distinct services that call this service (fan-in)
    # ADS: count of distinct services this service calls (fan-out)
    ais: dict = {svc: 0 for svc in nodes}
    ads: dict = {svc: 0 for svc in nodes}
    ais_callers: dict = {svc: set() for svc in nodes}  # track unique callers
    ads_callees: dict = {svc: set() for svc in nodes}  # track unique callees

    for e in edges:
        caller, callee = e["caller"], e["callee"]
        if callee in ais_callers:
            ais_callers[callee].add(caller)
        if caller in ads_callees:
            ads_callees[caller].add(callee)

    for svc in nodes:
        ais[svc] = len(ais_callers[svc])
        ads[svc] = len(ads_callees[svc])
        nodes[svc]["ais"] = ais[svc]
        nodes[svc]["ads"] = ads[svc]
        nodes[svc]["acs"] = ais[svc] * ads[svc]
        denom = ais[svc] + ads[svc]
        nodes[svc]["sdp"] = round(ads[svc] / denom, 4) if denom > 0 else 0.0

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "summary": {
            "total_services":  len(nodes),
            "total_edges":     len(edges),
            "min_edge_calls":  MIN_EDGE_CALLS,
        },
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Loading parsed spans from {INPUT_FILE}...")
    with open(INPUT_FILE) as f:
        spans = json.load(f)
    print(f"  Loaded {len(spans)} spans")

    print("Building service graph...")
    graph = build_graph(spans)
    n = graph["summary"]["total_services"]
    e = graph["summary"]["total_edges"]
    print(f"  Nodes (services): {n}")
    print(f"  Edges (min {MIN_EDGE_CALLS} calls): {e}")

    print("\n  Service coupling summary:")
    print(f"  {'Service':<45} {'AIS':>4}  {'ADS':>4}  {'ACS':>4}  {'SDP':>6}  {'p99ms':>7}")
    for node in sorted(graph["nodes"], key=lambda x: -x["ais"]):
        print(
            f"  {node['service']:<45} {node['ais']:>4}  {node['ads']:>4}  "
            f"{node['acs']:>4}  {node['sdp']:>6.3f}  "
            f"{node['latency']['p99_ms']:>7.1f}"
        )

    output_file = os.path.join(OUTPUT_DIR, "graph-service.json")
    with open(output_file, "w") as f:
        json.dump(graph, f, indent=2)
    print(f"\n  Saved {output_file} ({os.path.getsize(output_file):,} bytes)")


if __name__ == "__main__":
    main()