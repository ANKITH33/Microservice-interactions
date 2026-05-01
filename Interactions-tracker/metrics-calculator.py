"""
metrics_calculator.py  (v2)

Compute final per-service and per-endpoint metrics by combining:
  - Endpoint aggregates from Zipkin traces (sole source of truth for endpoint-level)
  - Service-level Prometheus data (request rates, CPU, memory, time series)
  - Zipkin dependency call counts
  - Replica counts from Kubernetes (falls back to 1)

Metrics computed
────────────────
Endpoint level (from Zipkin):
  • Bottleneck score  = tail_ratio × AIS × request_rate
  • Bottleneck score adj = tail_ratio × (AIS/replicas) × request_rate
  • Risk score        = failure_prob × (AIS / replicas)
  • tail_ratio        = p99 / p50
  • AIS               = afferent coupling (unique callers)
  • ADS               = efferent coupling (unique callees, from fanout)
  • ACS               = fan-out / (fan-out + fan-in) — absolute criticality
  • Error classification: rate_4xx, rate_5xx, rate_grpc, rate_timeout
  • Retry amplification factor (retry_factor) from Prometheus
  • Endpoint fan-out (avg downstream endpoints per call)
  • SLO compliance (service-level from Prometheus, endpoint uses trace p99)
  • Throughput vs latency data points (for curve plotting)

Service level:
  • All endpoint-level metrics aggregated (max/mean/sum)
  • SIUC: avg endpoint usage ratio across consumers
         For each consumer C of service S:
           ratio = (endpoints of S that C calls) / (total endpoints of S)
         SIUC = mean of those ratios
  • SIDC: NOTE — real SIDC requires OpenAPI schema similarity (cosine
          similarity of endpoint schemas).  We do NOT have schema data from
          Zipkin/Prometheus.  We mark sidc=null and explain the data gap
          rather than computing a meaningless proxy.
  • TSIC: computed as SIUC alone when SIDC is unavailable, and noted.
  • Coupling: AIS, ADS, ACS per service (from Zipkin dependency graph)
  • Prometheus: request_rate, error_rate, p50/p99, cpu, memory, retry_factor,
                slo_compliance_500ms, time_series
  • Zipkin dependency call counts per edge (previously unused — now included)
  • Replica count visualization data
  • CPU vs request rate data point (for correlation plot)
  • ACS standalone value (for dedicated chart)
  • worst_endpoint (highest bottleneck score endpoint per service)
  • p50 on bottleneck overview

Inputs
──────
  outputs/endpoint-aggregates.json   (from zipkin_parser.py)
  outputs/prometheus-processed.json  (from prometheus_collector.py)
  uploads/zipkin-dependencies.json   (raw Zipkin dependency call counts)

Outputs
───────
  outputs/metrics-endpoint.json
  outputs/metrics-service.json
  outputs/bottlenecks.json
"""

import json
import os
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path
import math

UPLOADS_DIR = Path(__file__).resolve().parent.parent / "baseline" / "outputs-baseline"
OUTPUT_DIR           = Path(__file__).resolve().parent / "outputs"
OBSERVATION_WINDOW_SEC = 600.0   # 10-minute collection window


# ── I/O helpers ────────────────────────────────────────────────────────────

def _load(filename: str):
    path = OUTPUT_DIR / filename
    with open(path) as f:
        return json.load(f)


def _load_upload(filename: str):
    path = UPLOADS_DIR / filename
    with open(path) as f:
        return json.load(f)


# ── Kubernetes replica counts ──────────────────────────────────────────────

def get_replica_counts() -> dict:
    """
    Fetch ready replica counts via kubectl.
    Falls back to replica_count=1 for all services if kubectl is unavailable.
    """
    replicas: dict = {}
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "deployments", "-n", "default",
                "-o",
                "jsonpath={range .items[*]}{.metadata.name}={.status.readyReplicas},{end}",
            ],
            capture_output=True, text=True, timeout=15,
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
    except FileNotFoundError:
        print("  WARNING: kubectl not found — defaulting all replicas to 1")
    except Exception as exc:
        print(f"  WARNING: Could not fetch replicas ({exc}) — defaulting to 1")
    return replicas


def _match_replicas(svc: str, replicas: dict) -> int:
    """Match Istio service name (e.g. 'adservice.default') to a deployment."""
    svc_name = svc.replace(".default", "").replace(".istio-system", "")
    if svc_name in replicas:
        return replicas[svc_name]
    for dep_name, count in replicas.items():
        if dep_name in svc_name or svc_name in dep_name:
            return count
    return 1


# ── Prometheus index helpers ───────────────────────────────────────────────

def _build_prom_index(prom: dict) -> dict:
    """
    Index Prometheus service metrics by both short name and .default-suffixed name.
    e.g. 'adservice' and 'adservice.default' → same entry.
    """
    index = prom.get("service_index", {})
    extended = {}
    for k, v in index.items():
        extended[k] = v
        short = k.replace(".default", "").replace(".istio-system", "")
        extended[short] = v
    return extended


# ── SIUC computation ───────────────────────────────────────────────────────

def compute_siuc(service: str, ep_agg: list) -> float:
    """
    Service Interface Usage Cohesion (corrected formula).

    For each consumer C of service S:
      ratio_C = (number of S's endpoints that C calls) / (total endpoints of S)
    SIUC = mean(ratio_C for all consumers C)

    Interpretation:
      1.0 = every consumer calls every endpoint (high usage cohesion)
      0.0 = each consumer calls only one endpoint (interface is fragmented)

    Data source: endpoint-aggregates.json — the 'callers' list per endpoint
    tells us which (caller_svc, caller_op) pairs call each endpoint.
    """
    # Endpoints belonging to this service
    svc_endpoints = [ep for ep in ep_agg if ep["service"] == service]
    num_endpoints = len(svc_endpoints)
    if num_endpoints == 0:
        return 0.0
    if num_endpoints == 1:
        return 1.0   # single endpoint → trivially cohesive

    # Map consumer service → set of this service's endpoints it calls
    consumer_endpoints: dict = defaultdict(set)
    for ep in svc_endpoints:
        op = ep["operation"]
        for caller in ep.get("callers", []):
            c_svc = caller.get("caller_svc")
            if c_svc:
                consumer_endpoints[c_svc].add(op)

    if not consumer_endpoints:
        return 0.0

    ratios = [
        len(ep_set) / num_endpoints
        for ep_set in consumer_endpoints.values()
    ]
    return round(sum(ratios) / len(ratios), 4)


# ── ACS computation helpers ────────────────────────────────────────────────

def build_call_graph(ep_agg: list) -> tuple:
    """
    Build fan-in and fan-out counts from endpoint aggregates.

    fan_in[svc]  = number of unique services calling svc
    fan_out[svc] = number of unique services svc calls (from callers inverse)

    Returns (fan_in, fan_out) as dicts keyed by service name.
    """
    fan_in:  dict = defaultdict(set)   # svc → set of caller services
    fan_out: dict = defaultdict(set)   # svc → set of callee services

    for ep in ep_agg:
        svc = ep["service"]
        for caller in ep.get("callers", []):
            c_svc = caller.get("caller_svc")
            if c_svc and c_svc != svc:
                fan_in[svc].add(c_svc)
                fan_out[c_svc].add(svc)

    return (
        {k: len(v) for k, v in fan_in.items()},
        {k: len(v) for k, v in fan_out.items()},
    )

def build_cp_indexes(cp_data: dict) -> tuple:
    """
    Build two lookup dicts from critical-paths.json:
 
    ep_weight[(svc, op)]  = appearance_count / total_traces
                            → float in [0, 1]
                            → 0.0 if endpoint never appears on critical path
 
    svc_weight[svc]       = appearance_count / total_traces
                            → float in [0, 1]
 
    Using appearance_count / total_traces (not raw count) ensures services
    that appear in more traces don't get an unfair advantage — a service
    appearing on the critical path of every trace gets weight=1.0,
    one appearing in half gets 0.5.
    """
    ep_weight  = {}
    svc_weight = {}
 
    for entry in cp_data.get("by_endpoint", []):
        key = (entry["service"], entry["operation"])
        ep_weight[key] = entry["appearance_weight"]   # already normalised
 
    for entry in cp_data.get("by_service", []):
        svc_weight[entry["service"]] = entry["appearance_weight"]
 
    return ep_weight, svc_weight

# ── Endpoint-level metrics ─────────────────────────────────────────────────

def _apply_cp_weight_to_scores(
    tail_ratio:     float,
    ais:            float,
    ais_adjusted:   float,
    req_rate:       float,
    svc:            str,
    op:             str,
    ep_cp_weight:   dict,
    svc_cp_weight:  dict,
) -> tuple:
    """
    Compute bottleneck scores with critical path weight applied.
 
    Formula:
        bottleneck_score = tail_ratio × max(AIS, 1) × req_rate
                           × (1 + cp_weight)
 
    where cp_weight = appearance_count / total_traces for this endpoint.
    Falls back to service-level weight if endpoint not found.
    Falls back to 0.0 (no adjustment) if neither found.
 
    Returns (bottleneck_score, bottleneck_score_adj) — both floats.
    """
    req_log = math.log1p(req_rate)

    # simple bounded normalization (no global stats needed)
    tail_n = min(tail_ratio / 5.0, 1.0)        # assume 5 is bad tail
    ais_n  = min(ais / 5.0, 1.0)               # cap AIS at 5
    req_n  = min(req_log / math.log1p(50), 1.0)  # cap at ~50 rps

    cp_weight = ep_cp_weight.get((svc, op), svc_cp_weight.get(svc, 0.0))

    score = (
        0.4 * tail_n +
        0.3 * ais_n +
        0.3 * req_n
    ) * (1 + cp_weight)

    score_adj = (
        0.4 * tail_n +
        0.3 * min(ais_adjusted / 5.0, 1.0) +
        0.3 * req_n
    ) * (1 + cp_weight)

    return round(score * 100, 2), round(score_adj * 100, 2)

def compute_endpoint_metrics(
    ep_agg:       list,
    prom:         dict,
    replicas:     dict,
    fan_in:       dict,
    fan_out:      dict,
    zipkin_deps:  list,
    ep_cp_weight:  dict = None,
    svc_cp_weight: dict = None,
) -> list:
    """
    Compute bottleneck score, risk score, coupling, and all new metrics
    for each endpoint.
    """
    if ep_cp_weight  is None: ep_cp_weight  = {}
    if svc_cp_weight is None: svc_cp_weight = {}

    prom_index = _build_prom_index(prom)

    dep_lookup: dict = {}
    for dep in zipkin_deps:
        parent_short = dep["parent"].replace(".default", "")
        child_short  = dep["child"].replace(".default", "")
        dep_lookup[(parent_short, child_short)] = dep["callCount"]

    results = []
    for ep in ep_agg:
        svc      = ep["service"]
        op       = ep["operation"]
        svc_short = svc.replace(".default", "")

        p50     = ep["latency"]["p50_ms"]
        p95     = ep["latency"]["p95_ms"]
        p99     = ep["latency"]["p99_ms"]
        mean_ms = ep["latency"]["mean_ms"]
        calls   = ep["call_count"]

        # ── Coupling ───────────────────────────────────────────────────────
        ais     = ep["unique_callers"]
        ads     = ep["fanout_avg"]
        ads_int = round(ads)

        acs = ais * ads_int

        # ── Request rate ───────────────────────────────────────────────────
        pm        = prom_index.get(svc, prom_index.get(svc_short, {}))
        prom_rate = pm.get("request_rate", 0.0)
        req_rate  = prom_rate if prom_rate > 0 else (calls / OBSERVATION_WINDOW_SEC)

        # ── Tail ratio ─────────────────────────────────────────────────────
        tail_ratio = (p99 / p50) if p50 > 0 else 1.0

        # ── Replica-adjusted AIS ───────────────────────────────────────────
        replica_count = _match_replicas(svc, replicas)
        ais_adjusted  = ais / max(replica_count, 1)

        # ── Bottleneck scores (critical path weighted) ─────────────────────
        cp_weight  = ep_cp_weight.get((svc, op), svc_cp_weight.get(svc, 0.0))
        multiplier = 1.0 + cp_weight

        bottleneck_score, bottleneck_score_adj = _apply_cp_weight_to_scores(
            tail_ratio, ais, ais_adjusted, req_rate,
            svc, op,
            ep_cp_weight  or {},
            svc_cp_weight or {},
        )

        # ── Risk score ─────────────────────────────────────────────────────
        error_rate   = ep["errors"]["rate"]
        failure_prob = min(error_rate + max(tail_ratio - 1, 0) / 10.0, 1.0)
        blast_radius = ais / max(replica_count, 1)
        risk_score   = round(failure_prob * blast_radius, 4)

        # ── SLO compliance ─────────────────────────────────────────────────
        def slo_approx(target_ms: float) -> float:
            if p99 <= target_ms: return 1.0
            if p50 > target_ms:  return 0.0
            span = p99 - p50
            if span <= 0: return 0.5
            frac = (target_ms - p50) / span
            return round(0.50 + frac * 0.49, 4)

        slo_200ms = slo_approx(200.0)
        slo_500ms = pm.get("slo_compliance_500ms", slo_approx(500.0))

        # ── Throughput vs latency ──────────────────────────────────────────
        throughput_latency = {
            "request_rate": round(req_rate, 4),
            "p50_ms":       p50,
            "p99_ms":       p99,
        }

        # ── Retry factor ───────────────────────────────────────────────────
        retry_factor = pm.get("retry_factor", 1.0)

        # ── Zipkin dep call counts ─────────────────────────────────────────
        dep_calls = {
            f"{svc_short}->{child}": cnt
            for (parent, child), cnt in dep_lookup.items()
            if parent == svc_short
        }

        results.append({
            "id":           f"{svc}::{op}",
            "service":      svc,
            "operation":    op,
            "protocol":     ep["protocol"],
            "call_count":   calls,
            "request_rate": round(req_rate, 4),
            "latency": {
                "p50_ms":   p50,
                "p95_ms":   p95,
                "p99_ms":   p99,
                "mean_ms":  mean_ms,
                "stdev_ms": ep["latency"]["stdev_ms"],
                "min_ms":   ep["latency"]["min_ms"],
                "max_ms":   ep["latency"]["max_ms"],
            },
            "errors": ep["errors"],
            "coupling": {
                "ais":          ais,
                "ads":          round(ads, 4),
                "acs":          acs,
                "ais_adjusted": round(ais_adjusted, 4),
                "callers":      ep.get("callers", []),
            },
            "fanout_avg":           ep["fanout_avg"],
            "tail_ratio":           round(tail_ratio, 4),
            "replica_count":        replica_count,
            "cp_weight":            round(cp_weight, 6),
            "bottleneck_score":     bottleneck_score,
            "bottleneck_score_adj": bottleneck_score_adj,
            "risk_score":           risk_score,
            "slo": {
                "slo_200ms": slo_200ms,
                "slo_500ms": round(float(slo_500ms), 4),
            },
            "throughput_latency":   throughput_latency,
            "retry_factor":         retry_factor,
            "dep_calls":            dep_calls,
        })

    results.sort(key=lambda x: -x["bottleneck_score"])
    return results


# ── Service-level metrics ──────────────────────────────────────────────────

def compute_service_metrics(
    ep_metrics:  list,
    prom:        dict,
    replicas:    dict,
    fan_in:      dict,
    fan_out:     dict,
    ep_agg:      list,
    zipkin_deps: list,
) -> list:
    """
    Aggregate endpoint metrics to service level and compute cohesion metrics.

    Coupling metrics
    ────────────────
    AIS  — Afferent coupling: number of unique services calling this service.
    ADS  — Efferent coupling: number of unique services this service calls.
    ACS  — Absolute criticality: AIS / (AIS + ADS)
    SIUC — Service Interface Usage Cohesion (corrected formula, see compute_siuc)
    SIDC — NOT computed. Requires OpenAPI schema similarity data that is
           unavailable from Zipkin/Prometheus. Marked null.
    TSIC — Since SIDC is null, TSIC = SIUC alone (noted in output).

    Time series
    ───────────
    All Prometheus time series are included per service for:
      request_rate, p99_latency, error_rate, cpu_usage, memory_usage, etc.

    Replica count
    ─────────────
    Included as a standalone field for replica visualisation.

    CPU vs request rate
    ───────────────────
    A {cpu_cores, request_rate} data point per service for correlation plot.

    Zipkin dependency call counts
    ─────────────────────────────
    All dependency edges (parent/child/callCount) where this service is parent
    or child are included — previously these were only in the dependency JSON.
    """
    prom_index = _build_prom_index(prom)
    time_series = prom.get("time_series", {})

    # Index endpoint metrics by service
    ep_by_svc: dict = defaultdict(list)
    for ep in ep_metrics:
        ep_by_svc[ep["service"]].append(ep)

    # Collect all services seen in either endpoint data or Prometheus
    all_services = set(ep_by_svc.keys())
    for svc in prom.get("service_index", {}).keys():
        # Normalise to .default form
        if ".default" not in svc:
            all_services.add(svc + ".default")
        else:
            all_services.add(svc)

    # Build zipkin dependency lookup per service
    dep_by_svc: dict = defaultdict(list)
    for dep in zipkin_deps:
        dep_by_svc[dep["parent"]].append(dep)
        dep_by_svc[dep["child"]].append(dep)   # also index by child

    # Build per-service time series (subset of global time series)
    def _get_ts(metric_key: str, svc_short: str) -> list:
        """Get time series for a service from prom range data."""
        series_list = time_series.get(metric_key, [])
        for s in series_list:
            if s["service"] == svc_short:
                return [
                    {"ts": t, "value": v}
                    for t, v in zip(s["timestamps"], s["values"])
                ]
        return []

    results = []
    for svc in sorted(all_services):
        svc_short = svc.replace(".default", "")
        pm        = prom_index.get(svc, prom_index.get(svc_short, {}))
        svc_eps   = ep_by_svc.get(svc, [])

        # ── Bottleneck / risk aggregation from endpoints ───────────────────
        if svc_eps:
            bottleneck_score     = max(ep["bottleneck_score"]     for ep in svc_eps)
            bottleneck_score_adj = max(ep["bottleneck_score_adj"] for ep in svc_eps)
            worst_ep             = max(svc_eps, key=lambda e: e["bottleneck_score"])
            risk_score           = max(ep["risk_score"]           for ep in svc_eps)
            tail_ratio           = max(ep["tail_ratio"]           for ep in svc_eps)
        else:
            bottleneck_score = bottleneck_score_adj = risk_score = tail_ratio = 0.0
            worst_ep = None

        # ── Latency from endpoints (worst-case across endpoints) ───────────
        if svc_eps:
            p50_ms  = max(ep["latency"]["p50_ms"]  for ep in svc_eps)
            p95_ms  = max(ep["latency"]["p95_ms"]  for ep in svc_eps)
            p99_ms  = max(ep["latency"]["p99_ms"]  for ep in svc_eps)
            mean_ms = round(sum(ep["latency"]["mean_ms"] for ep in svc_eps) / len(svc_eps), 3)
        else:
            p50_ms = p95_ms = p99_ms = mean_ms = 0.0

        # ── Error aggregation from endpoints ───────────────────────────────
        total_calls = sum(ep["call_count"] for ep in svc_eps)
        err_total   = sum(ep["errors"]["total"]       for ep in svc_eps)
        err_4xx     = sum(ep["errors"]["count_4xx"]   for ep in svc_eps)
        err_5xx     = sum(ep["errors"]["count_5xx"]   for ep in svc_eps)
        err_grpc    = sum(ep["errors"]["count_grpc"]  for ep in svc_eps)
        err_timeout = sum(ep["errors"]["count_timeout"] for ep in svc_eps)
        n = max(total_calls, 1)

        # ── Coupling (service-level AIS, ADS, ACS) ─────────────────────────
        ais = fan_in.get(svc, fan_in.get(svc_short, 0))
        ads = fan_out.get(svc, fan_out.get(svc_short, 0))
        acs = ais * ads

        # ── Cohesion metrics ───────────────────────────────────────────────
        # SIUC: correctly computed as avg endpoint-usage ratio per consumer
        siuc = compute_siuc(svc, ep_agg)

        # SIDC: requires OpenAPI schema data — NOT available from traces.
        # We do NOT compute a proxy (the old 1/num_endpoints formula was
        # conceptually wrong). We mark null with a clear explanation.
        sidc = None  # data unavailable — needs endpoint schema similarity

        # TSIC: use SIUC as the sole available cohesion measure
        tsic = siuc

        num_endpoints = len(svc_eps)

        # ── Replica count ──────────────────────────────────────────────────
        replica_count = _match_replicas(svc, replicas)

        # ── SLO compliance ─────────────────────────────────────────────────
        slo_500ms = pm.get("slo_compliance_500ms", 0.0)
        slo_200ms = round(sum(ep["slo"]["slo_200ms"] for ep in svc_eps) / len(svc_eps), 4) \
                    if svc_eps else 0.0

        # ── Retry amplification factor ─────────────────────────────────────
        retry_factor = pm.get("retry_factor", 1.0)

        # ── CPU vs request_rate data point ─────────────────────────────────
        cpu_vs_rps = {
            "cpu_cores":    pm.get("cpu_cores",    0.0),
            "request_rate": pm.get("request_rate", 0.0),
        }

        # ── Zipkin dependency call counts ──────────────────────────────────
        zipkin_edges = []
        for dep in zipkin_deps:
            p = dep["parent"].replace(".default", "")
            c = dep["child"].replace(".default", "")
            if p == svc_short or c == svc_short:
                zipkin_edges.append({
                    "parent":    dep["parent"],
                    "child":     dep["child"],
                    "callCount": dep["callCount"],
                    "direction": "outgoing" if p == svc_short else "incoming",
                })

        # ── Time series (Prometheus range data) ───────────────────────────
        svc_time_series = {
            "request_rate_ts":  _get_ts("request_rate",  svc_short),
            "p99_latency_ts":   _get_ts("p99_latency",   svc_short),
            "error_rate_ts":    _get_ts("error_rate",    svc_short),
            "error_5xx_ts":     _get_ts("error_5xx",     svc_short),
            "retry_rate_ts":    _get_ts("retry_rate",    svc_short),
            "cpu_usage_ts":     _get_ts("cpu_usage",     svc_short),
            "memory_usage_ts":  _get_ts("memory_usage",  svc_short),
        }

        results.append({
            "service":       svc,
            "num_endpoints": num_endpoints,
            "replica_count": replica_count,
            "call_count":    total_calls,
            # Latency (from Zipkin traces, worst endpoint)
            "latency": {
                "p50_ms":  p50_ms,
                "p95_ms":  p95_ms,
                "p99_ms":  p99_ms,
                "mean_ms": mean_ms,
            },
            # Error breakdown (from Zipkin traces)
            "errors": {
                "total":           err_total,
                "count_4xx":       err_4xx,
                "count_5xx":       err_5xx,
                "count_grpc":      err_grpc,
                "count_timeout":   err_timeout,
                "rate":            round(err_total   / n, 4),
                "rate_4xx":        round(err_4xx     / n, 4),
                "rate_5xx":        round(err_5xx     / n, 4),
                "rate_grpc":       round(err_grpc    / n, 4),
                "rate_timeout":    round(err_timeout / n, 4),
            },
            # Coupling (from call graph)
            "coupling": {
                "ais": ais,
                "ads": ads,
                "acs": acs,
            },
            # Cohesion (SIDC=null due to data unavailability)
            "cohesion": {
                "siuc":          siuc,
                "sidc":          sidc,          # null — needs schema data
                "tsic":          tsic,           # = siuc when sidc unavailable
                "sidc_note":     "SIDC requires OpenAPI/schema similarity data "
                                 "unavailable from Zipkin/Prometheus.",
                "num_endpoints": num_endpoints,
            },
            # Prometheus-sourced (service-level)
            "prometheus": {
                "request_rate":         pm.get("request_rate",         0.0),
                "total_requests":       pm.get("total_requests",       0.0),
                "error_rate":           pm.get("error_rate",           0.0),
                "error_5xx_rate":       pm.get("error_5xx_rate",       0.0),
                "error_4xx_rate":       pm.get("error_4xx_rate",       0.0),
                "retry_rate":           pm.get("retry_rate",           0.0),
                "timeout_rate":         pm.get("timeout_rate",         0.0),
                "p50_ms":               pm.get("p50_ms",               0.0),
                "p99_ms":               pm.get("p99_ms",               0.0),
                "cpu_cores":            pm.get("cpu_cores",            0.0),
                "memory_mb":            pm.get("memory_mb",            0.0),
                "slo_compliance_500ms": slo_500ms,
                "retry_factor":         retry_factor,
            },
            "slo": {
                "slo_200ms": slo_200ms,
                "slo_500ms": slo_500ms,
            },
            "tail_ratio":            round(tail_ratio, 4),
            "bottleneck_score":      bottleneck_score,
            "bottleneck_score_adj":  bottleneck_score_adj,
            "risk_score":            risk_score,
            "worst_endpoint":        worst_ep["operation"] if worst_ep else None,
            "worst_endpoint_details": {
                "operation":         worst_ep["operation"],
                "bottleneck_score":  worst_ep["bottleneck_score"],
                "p50_ms":            worst_ep["latency"]["p50_ms"],
                "p99_ms":            worst_ep["latency"]["p99_ms"],
                "tail_ratio":        worst_ep["tail_ratio"],
                "ais":               worst_ep["coupling"]["ais"],
                "request_rate":      worst_ep["request_rate"],
            } if worst_ep else None,
            # CPU vs RPS correlation data point
            "cpu_vs_rps":            cpu_vs_rps,
            # ACS standalone value for chart
            "acs_standalone":        acs,
            # Retry amplification factor
            "retry_factor":          retry_factor,
            # Zipkin dependency edges (call counts)
            "zipkin_edges":          zipkin_edges,
            # Time series (Prometheus range)
            "time_series":           svc_time_series,
        })

    results.sort(key=lambda x: -x["bottleneck_score"])
    return results


# ── Bottleneck summary ─────────────────────────────────────────────────────

def build_bottlenecks(svc_metrics: list, ep_metrics: list) -> dict:
    """
    Build a focused bottleneck report combining:
      - Top services by bottleneck score
      - Top endpoints by bottleneck score
      - Risk severity tiers (critical / high / medium / low)
      - Worst endpoint per service (including p50 on overview)
      - ACS standalone chart data
      - Replica count visualisation data
      - CPU vs RPS correlation data
    """
    CRITICAL_THRESHOLD = 10.0
    HIGH_THRESHOLD     = 3.0
    MEDIUM_THRESHOLD   = 1.0

    def severity(score: float) -> str:
        if score >= CRITICAL_THRESHOLD:
            return "critical"
        if score >= HIGH_THRESHOLD:
            return "high"
        if score >= MEDIUM_THRESHOLD:
            return "medium"
        return "low"

    top_services = [
        {
            "service":               m["service"],
            "bottleneck_score":      m["bottleneck_score"],
            "bottleneck_score_adj":  m["bottleneck_score_adj"],
            "risk_score":            m["risk_score"],
            "severity":              severity(m["bottleneck_score"]),
            "p50_ms":                m["latency"]["p50_ms"],
            "p99_ms":                m["latency"]["p99_ms"],
            "tail_ratio":            m["tail_ratio"],
            "ais":                   m["coupling"]["ais"],
            "acs":                   m["coupling"]["acs"],
            "worst_endpoint":        m["worst_endpoint"],
            "worst_endpoint_details": m.get("worst_endpoint_details"),
            "replica_count":         m["replica_count"],
            "request_rate":          m["prometheus"]["request_rate"],
            "error_rate":            m["errors"]["rate"],
            "retry_factor":          m["retry_factor"],
            "slo_200ms":             m["slo"]["slo_200ms"],
            "slo_500ms":             m["slo"]["slo_500ms"],
        }
        for m in svc_metrics
    ]

    top_endpoints = [
        {
            "service":              ep["service"],
            "operation":            ep["operation"],
            "bottleneck_score":     ep["bottleneck_score"],
            "bottleneck_score_adj": ep["bottleneck_score_adj"],
            "risk_score":           ep["risk_score"],
            "severity":             severity(ep["bottleneck_score"]),
            "p50_ms":               ep["latency"]["p50_ms"],
            "p99_ms":               ep["latency"]["p99_ms"],
            "tail_ratio":           ep["tail_ratio"],
            "ais":                  ep["coupling"]["ais"],
            "acs":                  ep["coupling"]["acs"],
            "fanout_avg":           ep["fanout_avg"],
            "request_rate":         ep["request_rate"],
            "error_rate":           ep["errors"]["rate"],
            "rate_4xx":             ep["errors"]["rate_4xx"],
            "rate_5xx":             ep["errors"]["rate_5xx"],
            "rate_timeout":         ep["errors"]["rate_timeout"],
            "slo_200ms":            ep["slo"]["slo_200ms"],
            "slo_500ms":            ep["slo"]["slo_500ms"],
            "retry_factor":         ep["retry_factor"],
        }
        for ep in ep_metrics[:20]
    ]

    # ACS standalone chart data (all services sorted by ACS)
    acs_chart = sorted(
        [{"service": m["service"], "acs": m["coupling"]["acs"],
          "ais": m["coupling"]["ais"], "ads": m["coupling"]["ads"]}
         for m in svc_metrics],
        key=lambda x: -x["acs"]
    )

    # Replica count visualisation
    replica_chart = sorted(
        [{"service": m["service"], "replica_count": m["replica_count"],
          "ais": m["coupling"]["ais"], "bottleneck_score": m["bottleneck_score"]}
         for m in svc_metrics],
        key=lambda x: -x["ais"]
    )

    # CPU vs RPS correlation data
    cpu_rps_chart = [
        {
            "service":      m["service"],
            "cpu_cores":    m["cpu_vs_rps"]["cpu_cores"],
            "request_rate": m["cpu_vs_rps"]["request_rate"],
        }
        for m in svc_metrics
        if m["cpu_vs_rps"]["cpu_cores"] > 0 or m["cpu_vs_rps"]["request_rate"] > 0
    ]

    # Severity breakdown for dependency graph legend
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for s in top_services:
        severity_counts[s["severity"]] += 1

    return {
        "top_services":      top_services,
        "top_endpoints":     top_endpoints,
        "acs_chart":         acs_chart,
        "replica_chart":     replica_chart,
        "cpu_rps_chart":     cpu_rps_chart,
        "severity_counts":   severity_counts,
        "thresholds": {
            "critical": CRITICAL_THRESHOLD,
            "high":     HIGH_THRESHOLD,
            "medium":   MEDIUM_THRESHOLD,
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading endpoint aggregates (Zipkin source of truth)…")
    ep_agg = _load("endpoint-aggregates.json")
    print(f"  {len(ep_agg)} endpoints loaded")

    print("Loading Prometheus processed data…")
    prom = _load("prometheus-processed.json")
    print(f"  {len(prom.get('services', []))} services in Prometheus")

    print("Loading Zipkin dependency call counts…")
    zipkin_deps = _load_upload("zipkin-dependencies.json")
    print(f"  {len(zipkin_deps)} dependency edges")

    print("Loading critical path data…")
    cp_data = _load("critical-paths.json")

    print("Fetching replica counts from Kubernetes…")
    replicas = get_replica_counts()

    print("Building call graph…")
    fan_in, fan_out = build_call_graph(ep_agg)
    print(f"  fan_in  (AIS): {dict(sorted(fan_in.items()))}")
    print(f"  fan_out (ADS): {dict(sorted(fan_out.items()))}")

    print("\nComputing endpoint-level metrics…")
    ep_weight, svc_weight = build_cp_indexes(cp_data)

    ep_metrics = compute_endpoint_metrics(
        ep_agg, prom, replicas, fan_in, fan_out, zipkin_deps,
        ep_cp_weight=ep_weight,
        svc_cp_weight=svc_weight,
    )

    print(f"\n  Top 10 endpoints by bottleneck score:")
    print(f"  {'Operation':<55} {'Score':>8}  {'Adj':>8}  {'p50ms':>7}  "
          f"{'p99ms':>7}  {'AIS':>4}  {'ACS':>5}  {'risk':>7}  {'slo200':>7}")
    print(f"  {'-'*55} {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*4}  "
          f"{'-'*5}  {'-'*7}  {'-'*7}")
    for m in ep_metrics[:10]:
        print(
            f"  {m['operation'][:54]:<55} {m['bottleneck_score']:>8.2f}  "
            f"{m['bottleneck_score_adj']:>8.2f}  "
            f"{m['latency']['p50_ms']:>7.1f}  {m['latency']['p99_ms']:>7.1f}  "
            f"{m['coupling']['ais']:>4}  {m['coupling']['acs']:>5.3f}  "
            f"{m['risk_score']:>7.4f}  {m['slo']['slo_200ms']:>7.4f}"
        )

    out_ep = OUTPUT_DIR / "metrics-endpoint.json"
    with open(out_ep, "w") as f:
        json.dump(ep_metrics, f, indent=2)
    print(f"\n  Saved {out_ep} ({out_ep.stat().st_size:,} bytes)")

    print("\nAggregating service-level metrics…")
    svc_metrics = compute_service_metrics(
        ep_metrics, prom, replicas, fan_in, fan_out, ep_agg, zipkin_deps
    )

    print(f"\n  {'Service':<35} {'Score':>8}  {'Adj':>8}  {'Risk':>7}  "
          f"{'SIUC':>6}  {'AIS':>4}  {'ACS':>5}  {'Reps':>5}  "
          f"{'p50':>6}  {'p99':>7}  {'Worst endpoint'}")
    print(f"  {'-'*35} {'-'*8}  {'-'*8}  {'-'*7}  {'-'*6}  {'-'*4}  "
          f"{'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*30}")
    for m in svc_metrics:
        print(
            f"  {m['service']:<35} {m['bottleneck_score']:>8.2f}  "
            f"{m['bottleneck_score_adj']:>8.2f}  {m['risk_score']:>7.4f}  "
            f"{m['cohesion']['siuc']:>6.3f}  {m['coupling']['ais']:>4}  "
            f"{m['coupling']['acs']:>5.3f}  {m['replica_count']:>5}  "
            f"{m['latency']['p50_ms']:>6.1f}  {m['latency']['p99_ms']:>7.1f}  "
            f"{str(m['worst_endpoint'])[:35]}"
        )

    out_svc = OUTPUT_DIR / "metrics-service.json"
    with open(out_svc, "w") as f:
        json.dump(svc_metrics, f, indent=2)
    print(f"\n  Saved {out_svc} ({out_svc.stat().st_size:,} bytes)")

    print("\nBuilding bottleneck report…")
    bottlenecks = build_bottlenecks(svc_metrics, ep_metrics)
    out_bn = OUTPUT_DIR / "bottlenecks.json"
    with open(out_bn, "w") as f:
        json.dump(bottlenecks, f, indent=2)
    print(f"  Saved {out_bn} ({out_bn.stat().st_size:,} bytes)")

    print("\n  Severity breakdown:")
    for sev, cnt in bottlenecks["severity_counts"].items():
        print(f"    {sev}: {cnt}")

    print("\n  ACS chart (top 5 by criticality):")
    for e in bottlenecks["acs_chart"][:5]:
        print(f"    {e['service']:<35}  ACS={e['acs']:.3f}  AIS={e['ais']}  ADS={e['ads']}")

    print("\n  CPU vs RPS correlation data:")
    for e in bottlenecks["cpu_rps_chart"]:
        print(f"    {e['service']:<35}  cpu={e['cpu_cores']:.5f}  rps={e['request_rate']:.3f}")


if __name__ == "__main__":
    main()
