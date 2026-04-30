"""
graph_endpoint_builder.py

Builds a weighted endpoint-level dependency graph from parsed spans.

Design notes:
  - Nodes  = (service, normalized_operation) pairs
  - Edges  = (caller_svc, caller_op) → (callee_svc, callee_op)
  - Route normalization has already been applied by zipkin_parser.py so
    /product/OLJCESPC7Z and /product/1YMWWN1N4O both map to /product/{id}
    and appear as a single node.  This is the critical step that makes the
    endpoint graph meaningful instead of having hundreds of duplicate nodes.
  - MIN_EDGE_CALLS = 10: same rationale as graph_service_builder.
  - AIS/ADS/SDP are computed at endpoint granularity: they measure coupling
    at the operation level rather than service level.

Input:  outputs/parsed-spans.json
Output: outputs/graph-endpoint.json
"""

import json
import os
import statistics

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_DIR   = os.path.join(os.path.dirname(CURRENT_DIR), "baseline", "outputs-baseline")
OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
INPUT_FILE = os.path.join(OUTPUT_DIR, "parsed-spans.json")

MIN_EDGE_CALLS = 10


def percentile(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return s[idx]


def build_graph(spans: list) -> dict:

    # ── Per-endpoint aggregation ───────────────────────────────────────────
    ep_durations: dict = {}
    ep_errors:    dict = {}
    ep_calls:     dict = {}

    for s in spans:
        key = (s["service"], s["operation"])
        ep_durations.setdefault(key, []).append(s["duration_ms"])
        ep_errors.setdefault(key, 0)
        ep_calls.setdefault(key, 0)
        ep_calls[key] += 1
        if s["is_error"]:
            ep_errors[key] = ep_errors.get(key, 0) + 1

    # ── Build nodes ────────────────────────────────────────────────────────
    nodes: dict = {}
    for (svc, op), durs in ep_durations.items():
        node_id = f"{svc}:{op}"
        nodes[node_id] = {
            "id":          node_id,
            "service":     svc,
            "operation":   op,
            "call_count":  ep_calls[(svc, op)],
            "error_count": ep_errors.get((svc, op), 0),
            "error_rate":  round(
                ep_errors.get((svc, op), 0) / max(ep_calls[(svc, op)], 1), 4
            ),
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
        if not s.get("caller_svc") or not s.get("caller_op"):
            continue
        key = (s["caller_svc"], s["caller_op"], s["service"], s["operation"])
        if key not in edge_data:
            edge_data[key] = {"durations": [], "errors": 0}
        edge_data[key]["durations"].append(s["duration_ms"])
        if s["is_error"]:
            edge_data[key]["errors"] += 1

    edges = []
    for (csvc, cop, esvc, eop), data in edge_data.items():
        durs = data["durations"]
        if len(durs) < MIN_EDGE_CALLS:
            continue
        edges.append({
            "caller_id":   f"{csvc}:{cop}",
            "callee_id":   f"{esvc}:{eop}",
            "caller_svc":  csvc,
            "caller_op":   cop,
            "callee_svc":  esvc,
            "callee_op":   eop,
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

    # ── Endpoint-level coupling metrics ────────────────────────────────────
    ais_callers: dict = {nid: set() for nid in nodes}
    ads_callees: dict = {nid: set() for nid in nodes}

    for e in edges:
        if e["callee_id"] in ais_callers:
            ais_callers[e["callee_id"]].add(e["caller_id"])
        if e["caller_id"] in ads_callees:
            ads_callees[e["caller_id"]].add(e["callee_id"])

    for nid in nodes:
        ais = len(ais_callers[nid])
        ads = len(ads_callees[nid])
        nodes[nid]["ais"] = ais
        nodes[nid]["ads"] = ads
        nodes[nid]["acs"] = ais * ads
        denom = ais + ads
        nodes[nid]["sdp"] = round(ads / denom, 4) if denom > 0 else 0.0

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "summary": {
            "total_endpoints": len(nodes),
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

    print("Building endpoint graph...")
    graph = build_graph(spans)
    n = graph["summary"]["total_endpoints"]
    e = graph["summary"]["total_edges"]
    print(f"  Nodes (endpoints): {n}")
    print(f"  Edges (min {MIN_EDGE_CALLS} calls): {e}")

    # Show normalized endpoint list — confirms /product/{id} collapsing
    print("\n  All endpoints (confirms route normalization):")
    for node in sorted(graph["nodes"], key=lambda x: -x["call_count"]):
        print(
            f"  {node['service']:<35} {node['operation']:<55} "
            f"calls={node['call_count']:>5}  ais={node['ais']}  "
            f"p99={node['latency']['p99_ms']:.1f}ms"
        )

    output_file = os.path.join(OUTPUT_DIR, "graph-endpoint.json")
    with open(output_file, "w") as f:
        json.dump(graph, f, indent=2)
    print(f"\n  Saved {output_file} ({os.path.getsize(output_file):,} bytes)")


if __name__ == "__main__":
    main()