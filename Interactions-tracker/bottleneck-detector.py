"""
bottleneck_detector.py

Identifies bottleneck services and endpoints and produces a ranked report.

Detection logic:
  ┌──────────────────────────────────────────────────────────────────────────┐
  │ Bottleneck score  — performance degradation potential (computed in       │
  │                     metrics_calculator.py at endpoint level; aggregated  │
  │                     to service level as max(endpoint scores)).           │
  │                                                                          │
  │ Risk score        — failure probability × blast radius.                  │
  │                     Separate from bottleneck score: a service can have   │
  │                     low latency (low bottleneck) but 100% error rate     │
  │                     (high risk).                                         │
  │                                                                          │
  │ Severity thresholds:                                                     │
  │   high   : bottleneck_score > 5000  OR  risk_score > 0.3                │
  │   medium  : bottleneck_score > 1000  OR  risk_score > 0.1               │
  │   low    : everything else                                               │
  │                                                                          │
  │ Critical path     — DFS from entry-point services (those that have no    │
  │                     callers in the graph) maximising cumulative p99      │
  │                     latency.  The critical path is the sequence of       │
  │                     services whose combined tail latency is worst.       │
  └──────────────────────────────────────────────────────────────────────────┘

Input:  outputs/metrics-service.json
        outputs/metrics-endpoint.json
        outputs/graph-service.json
Output: outputs/bottlenecks.json
"""

import json
import os
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = CURRENT_DIR / "outputs"

# ── Configurable thresholds ────────────────────────────────────────────────
TAIL_RATIO_THRESHOLD   = 2.0   # p99/p50 above this → high tail latency flag
HIGH_AIS_THRESHOLD     = 2     # AIS above this with low replicas → high fan-in flag
HIGH_ERROR_THRESHOLD   = 0.01  # 1% error rate → error flag
LOW_COHESION_THRESHOLD = 0.4   # TSIC below this → low cohesion flag

# How many top bottlenecks to surface in the output
TOP_N = 5


def _load(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path) as f:
        return json.load(f)


def _severity(bottleneck_score: float, risk_score: float) -> str:
    if bottleneck_score > 5000 or risk_score > 0.3:
        return "high"
    if bottleneck_score > 1000 or risk_score > 0.1:
        return "medium"
    return "low"


# ── Service bottlenecks ────────────────────────────────────────────────────

def detect_service_bottlenecks(svc_metrics: list) -> list:
    """
    Classify each service and generate human-readable reason strings.

    Reasons are generated independently for each detected condition so that
    the output is suitable for direct display in a monitoring dashboard.
    """
    output = []
    for m in svc_metrics:
        reasons = []

        # Latency shape
        if m["tail_ratio"] >= TAIL_RATIO_THRESHOLD:
            reasons.append(
                f"high tail latency — p99/p50 = {m['tail_ratio']:.1f}× "
                f"(p50={m['latency']['p50_ms']:.1f}ms, "
                f"p99={m['latency']['p99_ms']:.1f}ms)"
            )

        # Fan-in with low redundancy
        ais   = m["coupling"]["ais"]
        reps  = m["replica_count"]
        if ais >= HIGH_AIS_THRESHOLD:
            adj = round(ais / max(reps, 1), 2)
            reasons.append(
                f"high fan-in — AIS={ais} callers, {reps} replica(s) "
                f"→ effective AIS/replica = {adj:.2f}"
            )

        # Error rate
        if m["error_rate"] >= HIGH_ERROR_THRESHOLD:
            reasons.append(
                f"elevated error rate — {m['error_rate'] * 100:.2f}% of spans are errors"
            )

        # Cohesion
        tsic = m["cohesion"]["tsic"]
        sidc_str = f"{m['cohesion']['sidc']:.3f}" if m['cohesion']['sidc'] is not None else "n/a"
        if tsic < LOW_COHESION_THRESHOLD and m["cohesion"]["num_endpoints"] > 1:
            reasons.append(
                f"low cohesion — TSIC={tsic:.3f} with "
                f"{m['cohesion']['num_endpoints']} endpoints "
                f"(SIDC={sidc_str}, SIUC={m['cohesion']['siuc']:.3f})"
            )

        # Prometheus-level errors (control-plane view)
        prom_err = m["prometheus"].get("error_rate", 0.0)
        if prom_err > 0:
            reasons.append(
                f"Prometheus error rate = {prom_err:.4f} req/s "
                f"(control-plane view)"
            )

        output.append({
            "service":               m["service"],
            "severity":              _severity(m["bottleneck_score"], m["risk_score"]),
            "bottleneck_score":      m["bottleneck_score"],
            "bottleneck_score_adj":  m["bottleneck_score_adj"],
            "risk_score":            m["risk_score"],
            "reasons":               reasons,
            "latency":               m["latency"],
            "coupling":              m["coupling"],
            "cohesion":              m["cohesion"],
            "tail_ratio":            m["tail_ratio"],
            "replica_count":         m["replica_count"],
            "request_rate":          m["prometheus"].get("request_rate", 0.0),
            "error_rate":            m["error_rate"],
            "worst_endpoint":        m["worst_endpoint"],
            "prometheus":            m["prometheus"],
        })

    # Sort by bottleneck score descending (worst first)
    output.sort(key=lambda x: -x["bottleneck_score"])
    return output


# ── Endpoint bottlenecks ───────────────────────────────────────────────────

def detect_endpoint_bottlenecks(ep_metrics: list) -> list:
    output = []
    for m in ep_metrics:
        reasons = []

        if m["tail_ratio"] >= TAIL_RATIO_THRESHOLD:
            reasons.append(
                f"high tail latency — p99/p50 = {m['tail_ratio']:.1f}× "
                f"(p50={m['latency']['p50_ms']:.1f}ms, "
                f"p99={m['latency']['p99_ms']:.1f}ms)"
            )

        ais  = m["coupling"]["ais"]
        reps = m["replica_count"]
        if ais >= HIGH_AIS_THRESHOLD:
            reasons.append(
                f"high fan-in — AIS={ais} caller(s), {reps} replica(s)"
            )

        if m["error_rate"] >= HIGH_ERROR_THRESHOLD:
            reasons.append(
                f"elevated error rate — {m['error_rate'] * 100:.2f}%"
            )

        output.append({
            "endpoint":             m["id"],
            "service":              m["service"],
            "operation":            m["operation"],
            "severity":             _severity(m["bottleneck_score"], m["risk_score"]),
            "bottleneck_score":     m["bottleneck_score"],
            "bottleneck_score_adj": m["bottleneck_score_adj"],
            "risk_score":           m["risk_score"],
            "reasons":              reasons,
            "latency":              m["latency"],
            "coupling":             m["coupling"],
            "tail_ratio":           m["tail_ratio"],
            "call_count":           m["call_count"],
            "request_rate":         m["request_rate"],
            "error_rate":           m["error_rate"],
            "replica_count":        m["replica_count"],
        })

    output.sort(key=lambda x: -x["bottleneck_score"])
    return output


# ── Critical path ──────────────────────────────────────────────────────────

def load_critical_path(cp_data: dict) -> dict:
    """
    Use pre-computed critical path data from zipkin_parser.py.
    Returns the highest-latency path from by_path, already trace-aware.
    """
    by_path = cp_data.get("by_path", [])
    if not by_path:
        return {"path": [], "latencies_ms": [], "total_p99_ms": 0.0, "entry_points": []}

    # by_path is sorted by frequency; find highest latency path instead
    best = max(by_path, key=lambda x: x["latency_ms_max"])
    return {
        "path":          best["path"],
        "latencies_ms":  [],   # per-hop latencies not stored in by_path
        "total_p99_ms":  best["latency_ms_max"],
        "count":         best["count"],
        "latency_ms_mean": best["latency_ms_mean"],
        "total_traces":  cp_data.get("total_traces", 0),
        "total_skipped": cp_data.get("total_skipped", 0),
    }


# ── Main ───────────────────────────────────────────────────────────────────

def detect(svc_metrics: list, ep_metrics: list, cp_data: dict) -> dict:
    svc_bottlenecks = detect_service_bottlenecks(svc_metrics)
    ep_bottlenecks  = detect_endpoint_bottlenecks(ep_metrics)
    critical_path = load_critical_path(cp_data)

    return {
        "service_bottlenecks":  svc_bottlenecks[:TOP_N],
        "endpoint_bottlenecks": ep_bottlenecks[:TOP_N],
        "all_service_scores": [
            {
                "service":          m["service"],
                "bottleneck_score": m["bottleneck_score"],
                "risk_score":       m["risk_score"],
                "tsic":             m["cohesion"]["tsic"],
                "severity":         m["severity"],
            }
            for m in svc_bottlenecks
        ],
        "critical_path": critical_path,
        "thresholds": {
            "tail_ratio":    TAIL_RATIO_THRESHOLD,
            "high_ais":      HIGH_AIS_THRESHOLD,
            "high_error":    HIGH_ERROR_THRESHOLD,
            "low_cohesion":  LOW_COHESION_THRESHOLD,
        },
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading metrics and graph...")
    svc_metrics = _load("metrics-service.json")
    ep_metrics  = _load("metrics-endpoint.json")
    cp_data = _load("critical-paths.json")

    print("Detecting bottlenecks...")
    result = detect(svc_metrics, ep_metrics, cp_data)

    # ── Console report ─────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  TOP SERVICE BOTTLENECKS")
    print("═" * 70)
    for b in result["service_bottlenecks"]:
        sev = b["severity"].upper()
        print(
            f"\n  [{sev:<6}] {b['service']}"
            f"\n           score={b['bottleneck_score']:.2f}  "
            f"risk={b['risk_score']:.4f}  "
            f"tsic={b['cohesion']['tsic']:.3f}  "
            f"replicas={b['replica_count']}  "
            f"ais={b['coupling']['ais']}"
        )
        for r in b["reasons"]:
            print(f"           → {r}")
        if b["worst_endpoint"]:
            print(f"           → worst endpoint: {b['worst_endpoint']}")

    print("\n" + "═" * 70)
    print("  TOP ENDPOINT BOTTLENECKS")
    print("═" * 70)
    for b in result["endpoint_bottlenecks"]:
        sev = b["severity"].upper()
        print(
            f"\n  [{sev:<6}] {b['operation']}"
            f"\n           service={b['service']}  "
            f"score={b['bottleneck_score']:.2f}  "
            f"risk={b['risk_score']:.4f}  "
            f"p99={b['latency']['p99_ms']:.1f}ms  "
            f"calls={b['call_count']}  "
            f"replicas={b['replica_count']}"
        )
        for r in b["reasons"]:
            print(f"           → {r}")

    print("\n" + "═" * 70)
    print("  CRITICAL PATH (highest cumulative p99 latency)")
    print("═" * 70)
    cp = result["critical_path"]
    path_str = " → ".join(cp["path"])
    print(f"\n  Path:      {path_str}")
    print(f"  Max latency: {cp['total_p99_ms']:.1f}ms  "
      f"(seen {cp.get('count', 0)}x across {cp.get('total_traces', 0)} traces, "
      f"{cp.get('total_skipped', 0)} skipped)")

    print("\n" + "═" * 70)
    print("  ALL SERVICES — SCORE SUMMARY")
    print("═" * 70)
    print(
        f"  {'Service':<40} {'Score':>8}  {'Risk':>7}  "
        f"{'TSIC':>6}  {'Severity'}"
    )
    print(f"  {'-'*40} {'-'*8}  {'-'*7}  {'-'*6}  {'-'*8}")
    for s in result["all_service_scores"]:
        print(
            f"  {s['service']:<40} {s['bottleneck_score']:>8.2f}  "
            f"{s['risk_score']:>7.4f}  {s['tsic']:>6.3f}  {s['severity']}"
        )

    out = os.path.join(OUTPUT_DIR, "bottlenecks.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Saved {out}")


if __name__ == "__main__":
    main()