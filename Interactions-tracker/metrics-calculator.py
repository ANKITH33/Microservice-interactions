"""
metrics_calculator.py

Computes final metrics per service and per endpoint by combining:
  - Endpoint / service graphs  (AIS, ADS, ACS, SDP, latency from traces)
  - Prometheus data            (request rate, latency, error rate, CPU, memory)
  - Replica counts             (from kubectl; falls back to 1 if unavailable)

Key design decisions:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ 1. Bottleneck score is computed at ENDPOINT level first.                │
  │    Formula: tail_ratio × AIS × request_rate_per_second                 │
  │      - tail_ratio = p99 / p50  (how spiky the latency distribution is) │
  │      - AIS = afferent instability score (fan-in from unique callers)    │
  │      - request_rate = from Prometheus; falls back to call_count/window  │
  │    Service score = max(endpoint scores) — the worst endpoint drives it. │
  │                                                                         │
  │ 2. Risk score is separate from bottleneck score.                        │
  │    Bottleneck = performance degradation potential.                      │
  │    Risk = failure probability × blast radius.                           │
  │    Formula: failure_prob × (AIS / replicas)                             │
  │      failure_prob = error_rate + clip(tail_ratio-1, 0) / 10            │
  │                                                                         │
  │ 3. Cohesion metrics (SIDC, SIUC, TSIC) measure how focused a service   │
  │    is.  Low cohesion means the service does many unrelated things →     │
  │    harder to reason about, higher maintenance risk.                     │
  │      SIDC = 1/num_endpoints  (simple: fewer ops = more cohesive)       │
  │      SIUC = 1/(1+CV) where CV = stdev/mean of endpoint call counts     │
  │             (high CV means some ops are rarely used → low cohesion)    │
  │      TSIC = (SIDC + SIUC) / 2                                          │
  │                                                                         │
  │ 4. Replica-adjusted scores: AIS/replicas penalises high-AIS services   │
  │    that have only one replica (single point of failure).                │
  └─────────────────────────────────────────────────────────────────────────┘

Input:  outputs/graph-service.json
        outputs/graph-endpoint.json
        outputs/prometheus-processed.json
Output: outputs/metrics-service.json
        outputs/metrics-endpoint.json
"""

import json
import os
import statistics
import subprocess
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent

BASE_DIR = CURRENT_DIR.parent / "baseline" / "outputs-baseline"
OUTPUT_DIR = CURRENT_DIR / "outputs"

# Observation window used when Prometheus request rate is unavailable
# Matches the lookback window used during data collection (10 minutes)
OBSERVATION_WINDOW_SEC = 600.0


# ── I/O helpers ────────────────────────────────────────────────────────────

def _load(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path) as f:
        return json.load(f)


# ── Kubernetes replica counts ──────────────────────────────────────────────

def get_replica_counts() -> dict:
    """
    Fetch ready replica counts from the Kubernetes API via kubectl.

    Why replicas matter:
      A service with AIS=5 and 1 replica is far more dangerous than the same
      service with 5 replicas.  Replica-adjusted scores surface single points
      of failure.

    Falls back to replica_count=1 for all services if kubectl is unavailable
    (e.g. running the analysis offline).
    """
    replicas: dict = {}
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "deployments", "-n", "default",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}={.status.readyReplicas},{end}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for part in result.stdout.strip().rstrip(",").split(","):
                if "=" in part:
                    name, count_str = part.split("=", 1)
                    try:
                        replicas[name] = int(count_str) if count_str else 1
                    except ValueError:
                        replicas[name] = 1
            print(f"  Fetched replica counts for {len(replicas)} deployments")
            for dep, cnt in sorted(replicas.items()):
                print(f"    {dep}: {cnt}")
        else:
            print("  WARNING: kubectl failed — defaulting all replicas to 1")
            print(f"  stderr: {result.stderr[:200]}")
    except FileNotFoundError:
        print("  WARNING: kubectl not found — defaulting all replicas to 1")
    except Exception as exc:
        print(f"  WARNING: Could not fetch replicas ({exc}) — defaulting to 1")
    return replicas


def _match_replicas(svc: str, replicas: dict) -> int:
    """
    Match a service name (possibly with .default suffix) to a deployment name.

    Istio service names look like 'adservice.default'; deployment names look
    like 'adservice'.  We strip the namespace suffix and do a fuzzy match as
    a fallback.
    """
    svc_name = svc.replace(".default", "").replace(".istio-system", "")

    # Exact match first
    if svc_name in replicas:
        return replicas[svc_name]

    # Fuzzy: deployment name is a substring of service name or vice-versa
    for dep_name, count in replicas.items():
        if dep_name in svc_name or svc_name in dep_name:
            return count

    return 1  # safe default


# ── Prometheus request-rate lookup ────────────────────────────────────────

def _build_prom_index(prom: dict) -> dict:
    """
    Build a dict keyed by service name (both with and without .default)
    pointing to the Prometheus per-service stats dict.
    """
    index = prom.get("service_index", {})
    extended = {}
    for k, v in index.items():
        extended[k] = v
        short = k.replace(".default", "").replace(".istio-system", "")
        extended[short] = v
    return extended


# ── Endpoint metrics ───────────────────────────────────────────────────────

def compute_endpoint_metrics(
    ep_graph: dict,
    prom: dict,
    replicas: dict,
) -> list:
    """
    Compute bottleneck score, risk score, and coupling metrics per endpoint.

    Bottleneck score formula (computed here, NOT at service level):
      tail_ratio × AIS × request_rate

    Where:
      tail_ratio   = p99_ms / p50_ms   (latency distribution shape)
      AIS          = number of distinct callers of this endpoint
      request_rate = from Prometheus (preferred) or estimated from trace window
    """
    prom_index = _build_prom_index(prom)

    results = []
    for node in ep_graph["nodes"]:
        svc   = node["service"]
        op    = node["operation"]
        p50   = node["latency"]["p50_ms"]
        p99   = node["latency"]["p99_ms"]
        ais   = node["ais"]
        calls = node["call_count"]

        # ── Request rate ───────────────────────────────────────────────────
        # Prefer Prometheus rate (measured at control plane) over trace-derived
        # estimate because Prometheus captures all requests while Zipkin may
        # sample.
        pm = prom_index.get(svc, {})
        prom_rate = pm.get("request_rate", 0.0)
        req_rate  = prom_rate if prom_rate > 0 else (calls / OBSERVATION_WINDOW_SEC)

        # ── Tail ratio ─────────────────────────────────────────────────────
        # p99 / p50: a ratio of 1.0 means all requests take the same time
        # (perfectly predictable).  A ratio of 10 means the worst 1% of
        # requests are 10× slower than the median — a serious problem.
        tail_ratio = (p99 / p50) if p50 > 0 else 1.0

        # ── Replica-adjusted AIS ───────────────────────────────────────────
        replica_count = _match_replicas(svc, replicas)
        ais_adjusted  = ais / max(replica_count, 1)

        # ── Bottleneck score ───────────────────────────────────────────────
        # Core metric: combines latency shape, fan-in pressure, and throughput.
        # High score = this endpoint is both slow (spiky) and widely called.
        bottleneck_score     = round(tail_ratio * max(ais, 1) * req_rate, 4)
        # Replica-adjusted version: penalises single-replica high-AIS endpoints
        bottleneck_score_adj = round(
            tail_ratio * max(ais_adjusted, 0.1) * req_rate, 4
        )

        # ── Risk score at endpoint level ───────────────────────────────────
        error_rate   = node["error_rate"]
        failure_prob = min(error_rate + max(tail_ratio - 1, 0) / 10.0, 1.0)
        blast_radius = ais / max(replica_count, 1)
        risk_score   = round(failure_prob * blast_radius, 4)

        results.append({
            "id":          node["id"],
            "service":     svc,
            "operation":   op,
            "call_count":  calls,
            "error_rate":  error_rate,
            "latency": {
                "p50_ms":  p50,
                "p95_ms":  node["latency"]["p95_ms"],
                "p99_ms":  p99,
                "mean_ms": node["latency"]["mean_ms"],
            },
            "coupling": {
                "ais":          ais,
                "ads":          node["ads"],
                "acs":          node["acs"],
                "sdp":          node["sdp"],
                "ais_adjusted": round(ais_adjusted, 4),
            },
            "replica_count":         replica_count,
            "request_rate":          round(req_rate, 4),
            "tail_ratio":            round(tail_ratio, 4),
            "bottleneck_score":      bottleneck_score,
            "bottleneck_score_adj":  bottleneck_score_adj,
            "risk_score":            risk_score,
        })

    results.sort(key=lambda x: -x["bottleneck_score"])
    return results


# ── Service metrics ────────────────────────────────────────────────────────

def compute_service_metrics(
    svc_graph: dict,
    ep_metrics: list,
    prom: dict,
    replicas: dict,
) -> list:
    """
    Aggregate endpoint-level scores up to service level and add cohesion metrics.

    Aggregation rule: service bottleneck score = MAX of its endpoint scores.
    This is deliberate: the worst endpoint is the bottleneck of the service.
    Using the average would mask a single hot endpoint inside a mostly-idle service.

    Cohesion metrics added here:
      SIDC  — Service Interface Data Cohesion
              Approximated as 1/num_endpoints: a service with 1 operation is
              maximally cohesive (all traffic goes to the same operation).
      SIUC  — Service Interface Usage Cohesion
              1 / (1 + CV) where CV = stdev/mean of endpoint call counts.
              CV=0 means all endpoints used equally → high cohesion.
              CV=∞ means one endpoint gets all traffic   → low cohesion.
      TSIC  — Total Service Interface Cohesion = (SIDC + SIUC) / 2
    """
    prom_index = _build_prom_index(prom)

    # Index endpoint metrics by service for O(1) lookup
    ep_by_svc: dict = {}
    for ep in ep_metrics:
        ep_by_svc.setdefault(ep["service"], []).append(ep)

    results = []
    for node in svc_graph["nodes"]:
        svc      = node["service"]
        pm       = prom_index.get(svc, {})
        svc_eps  = ep_by_svc.get(svc, [])

        # ── Bottleneck score (aggregated from endpoints) ───────────────────
        if svc_eps:
            # MAX: the worst endpoint drives the service's bottleneck score
            bottleneck_score     = max(ep["bottleneck_score"]     for ep in svc_eps)
            bottleneck_score_adj = max(ep["bottleneck_score_adj"] for ep in svc_eps)
            worst_ep = max(svc_eps, key=lambda e: e["bottleneck_score"])
        else:
            bottleneck_score     = 0.0
            bottleneck_score_adj = 0.0
            worst_ep             = None

        replica_count = _match_replicas(svc, replicas)
        ais           = node["ais"]
        ads           = node["ads"]
        num_endpoints = len(svc_eps)

        # ── Cohesion metrics ───────────────────────────────────────────────
        # SIDC: 1 / num_endpoints.
        #   Rationale: a service with 10 operations is less cohesive than one
        #   with 1 operation because it handles more diverse responsibilities.
        sidc = round(1.0 / max(num_endpoints, 1), 4)

        # SIUC: usage evenness across endpoints.
        #   If all endpoints are called equally often the service has a clear,
        #   uniform interface.  Wildly uneven usage suggests some ops are
        #   vestigial or the service is over-burdened with unrelated concerns.
        if num_endpoints > 1:
            counts    = [ep["call_count"] for ep in svc_eps]
            mean_c    = sum(counts) / len(counts)
            if mean_c > 0:
                std_c = statistics.stdev(counts)
                cv    = std_c / mean_c          # coefficient of variation
                siuc  = round(1.0 / (1.0 + cv), 4)
            else:
                siuc = 0.0
        else:
            siuc = 1.0   # single endpoint → trivially cohesive

        tsic = round((sidc + siuc) / 2, 4)

        # ── Risk score ─────────────────────────────────────────────────────
        # Risk = failure probability × blast radius.
        #
        # failure_prob: combines observed error rate with tail-latency shape.
        #   High tail latency (p99 ≫ p50) suggests occasional pathological
        #   behaviour even if the error rate is formally 0.
        #
        # blast_radius: AIS / replicas.
        #   AIS=5 means 5 services depend on this one.
        #   With 1 replica there is no redundancy → blast radius is maximal.
        p50        = node["latency"]["p50_ms"]
        p99        = node["latency"]["p99_ms"]
        error_rate = node["error_rate"]
        tail_ratio = (p99 / p50) if p50 > 0 else 1.0

        failure_prob = min(error_rate + max(tail_ratio - 1, 0) / 10.0, 1.0)
        blast_radius = ais / max(replica_count, 1)
        risk_score   = round(failure_prob * blast_radius, 4)

        results.append({
            "service":       svc,
            "call_count":    node["call_count"],
            "replica_count": replica_count,
            "error_rate":    node["error_rate"],
            "latency": {
                "p50_ms":  p50,
                "p95_ms":  node["latency"]["p95_ms"],
                "p99_ms":  p99,
                "mean_ms": node["latency"]["mean_ms"],
            },
            "coupling": {
                "ais": ais,
                "ads": ads,
                "acs": node["acs"],
                "sdp": node["sdp"],
            },
            # Cohesion — how focused the service's responsibilities are
            "cohesion": {
                "sidc":          sidc,   # data cohesion  (1/num_endpoints)
                "siuc":          siuc,   # usage cohesion (1/(1+CV))
                "tsic":          tsic,   # total  = (sidc+siuc)/2
                "num_endpoints": num_endpoints,
            },
            # Prometheus-sourced metrics (control-plane measurements)
            "prometheus": {
                "request_rate": pm.get("request_rate", 0.0),
                "error_rate":   pm.get("error_rate",   0.0),
                "p50_ms":       pm.get("p50_ms",        0.0),
                "p99_ms":       pm.get("p99_ms",        0.0),
                "cpu_cores":    pm.get("cpu_cores",     0.0),
                "memory_mb":    pm.get("memory_mb",     0.0),
            },
            "tail_ratio":            round(tail_ratio, 4),
            "bottleneck_score":      bottleneck_score,
            "bottleneck_score_adj":  bottleneck_score_adj,
            "risk_score":            risk_score,
            "worst_endpoint":        worst_ep["operation"] if worst_ep else None,
        })

    results.sort(key=lambda x: -x["bottleneck_score"])
    return results


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading graphs and Prometheus data...")
    svc_graph = _load("graph-service.json")
    ep_graph  = _load("graph-endpoint.json")
    prom      = _load("prometheus-processed.json")

    print("Fetching replica counts from Kubernetes...")
    replicas = get_replica_counts()

    print("\nComputing endpoint-level metrics (bottleneck score at endpoint first)...")
    ep_metrics = compute_endpoint_metrics(ep_graph, prom, replicas)

    print(f"\n  Top 10 endpoints by bottleneck score:")
    print(f"  {'Operation':<55} {'Score':>8}  {'p99ms':>7}  {'AIS':>4}  {'req/s':>7}  {'risk':>7}")
    print(f"  {'-'*55} {'-'*8}  {'-'*7}  {'-'*4}  {'-'*7}  {'-'*7}")
    for m in ep_metrics[:10]:
        print(
            f"  {m['operation']:<55} {m['bottleneck_score']:>8.2f}  "
            f"{m['latency']['p99_ms']:>7.1f}  {m['coupling']['ais']:>4}  "
            f"{m['request_rate']:>7.3f}  {m['risk_score']:>7.4f}"
        )

    out_ep = os.path.join(OUTPUT_DIR, "metrics-endpoint.json")
    with open(out_ep, "w") as f:
        json.dump(ep_metrics, f, indent=2)
    print(f"\n  Saved {out_ep}")

    print("\nAggregating to service-level metrics (max endpoint score per service)...")
    svc_metrics = compute_service_metrics(svc_graph, ep_metrics, prom, replicas)

    print(f"\n  Top services by bottleneck score:")
    print(
        f"  {'Service':<40} {'Score':>8}  {'Risk':>7}  "
        f"{'TSIC':>6}  {'AIS':>4}  {'Reps':>5}  {'p99ms':>7}  {'Worst endpoint'}"
    )
    print(f"  {'-'*40} {'-'*8}  {'-'*7}  {'-'*6}  {'-'*4}  {'-'*5}  {'-'*7}  {'-'*30}")
    for m in svc_metrics:
        print(
            f"  {m['service']:<40} {m['bottleneck_score']:>8.2f}  "
            f"{m['risk_score']:>7.4f}  {m['cohesion']['tsic']:>6.3f}  "
            f"{m['coupling']['ais']:>4}  {m['replica_count']:>5}  "
            f"{m['latency']['p99_ms']:>7.1f}  {str(m['worst_endpoint'])[:40]}"
        )

    out_svc = os.path.join(OUTPUT_DIR, "metrics-service.json")
    with open(out_svc, "w") as f:
        json.dump(svc_metrics, f, indent=2)
    print(f"\n  Saved {out_svc}")


if __name__ == "__main__":
    main()