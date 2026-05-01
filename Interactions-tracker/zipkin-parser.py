"""
zipkin_parser.py  (v2)

Parse raw Zipkin traces into structured span records that serve as the
sole source of truth for ALL endpoint-level metrics.

Key design decisions
────────────────────
1. SERVER spans only — avoids double-counting with their CLIENT mirrors.
2. Route normalisation — collapses /product/OLJCESPC7Z → /product/{id}.
3. grpc.path is preferred over http.url which is preferred over span name.
4. Caller is resolved by walking the parentId chain within the same trace,
   skipping infrastructure services so the real application caller is found.
5. Error classification:
     - HTTP 4xx  → error_4xx  (client error)
     - HTTP 5xx  → error_5xx  (server error)
     - gRPC non-zero → grpc_error
     - response_flags UT/UF/UC → timeout
   All four categories propagate into per-endpoint aggregates.
6. Downstream call fan-out (endpoint_fanout) is computed per callee-endpoint
   by counting unique downstream endpoints called within each span's sub-tree.

Outputs
───────
  outputs/parsed-spans.json     — one record per SERVER span
  outputs/endpoint-aggregates.json — per-endpoint aggregated stats
"""

import json
import os
import re
import statistics
from collections import defaultdict
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
UPLOADS_DIR = Path(__file__).resolve().parent.parent / "baseline" / "outputs-baseline"
OUTPUT_DIR  = Path(__file__).resolve().parent / "outputs"

# ── Configuration ──────────────────────────────────────────────────────────
EXCLUDE_SERVICES = {"istio-ingressgateway.istio-system", "loadgenerator.default"}
MIN_EDGE_CALLS   = 3   # edges with fewer calls are noise

# ── Route normalisation patterns ───────────────────────────────────────────
_PRODUCT_ID_RE = re.compile(r"^[A-Z0-9]{8,12}$")
_NUMERIC_ID_RE = re.compile(r"^\d+$")
_UUID_RE       = re.compile(r"^[0-9a-f]{8,}(-[0-9a-f]{4,}){1,}$", re.I)

# Timeout response_flags emitted by Envoy
_TIMEOUT_FLAGS = {"UT", "UF", "UC", "UH", "URX"}


# ── Helpers ────────────────────────────────────────────────────────────────

def normalize_operation(span: dict) -> str:
    """Return a canonical operation name, normalising dynamic path segments."""
    tags = span.get("tags", {})

    # 1. gRPC path — already canonical
    grpc_path = tags.get("grpc.path", "")
    if grpc_path:
        return grpc_path

    # 2. HTTP URL — strip host + query, normalise IDs
    http_url = tags.get("http.url", "")
    if http_url:
        path = re.sub(r"^https?://[^/]+", "", http_url).split("?")[0] or "/"
        parts = path.split("/")
        normalised = []
        for part in parts:
            if not part:
                normalised.append(part)
            elif _PRODUCT_ID_RE.match(part) or _NUMERIC_ID_RE.match(part) or _UUID_RE.match(part):
                normalised.append("{id}")
            else:
                normalised.append(part)
        return "/".join(normalised) or "/"

    # 3. Span name (Istio-style "host:port/path")
    name = span.get("name", "unknown")
    if ":" in name and "/" in name:
        name = "/" + name.split("/", 1)[-1]
    return name or "unknown"


def get_service(span: dict) -> str:
    return span.get("localEndpoint", {}).get("serviceName", "unknown")


def classify_errors(span: dict) -> dict:
    """
    Return a dict of boolean error flags for this span.

    Categories
    ──────────
    is_4xx     HTTP 4xx response (client error — e.g. 404, 400)
    is_5xx     HTTP 5xx response (server error — e.g. 500, 503)
    is_grpc_err gRPC non-zero status (any gRPC error)
    is_timeout  Envoy response_flags indicate upstream timeout
    is_error    Union of all the above
    """
    tags = span.get("tags", {})

    status_code  = str(tags.get("http.status_code", "200"))
    grpc_status  = str(tags.get("grpc.status_code", "0"))
    resp_flags   = set(tags.get("response_flags", "").split(","))

    is_4xx      = status_code.startswith("4")
    is_5xx      = status_code.startswith("5")
    is_grpc_err = grpc_status not in ("", "0")
    is_timeout  = bool(resp_flags & _TIMEOUT_FLAGS)
    is_error    = is_4xx or is_5xx or is_grpc_err or is_timeout

    return {
        "is_4xx":      is_4xx,
        "is_5xx":      is_5xx,
        "is_grpc_err": is_grpc_err,
        "is_timeout":  is_timeout,
        "is_error":    is_error,
    }


# ── Core parsing ───────────────────────────────────────────────────────────

def parse_traces(traces: list) -> list:
    """
    Convert raw Zipkin traces into flat span records (one per SERVER span).

    For each SERVER span we record:
      - service, operation, caller_svc, caller_op
      - duration_us / duration_ms
      - error classification (4xx, 5xx, gRPC, timeout)
      - protocol (grpc | http)
      - trace_id / span_id / parent_id
    """
    records = []

    for trace in traces:
        if not trace:
            continue

        trace_id    = trace[0].get("traceId", "")
        spans_by_id = {s["id"]: s for s in trace}

        for span in trace:
            if span.get("kind") != "SERVER":
                continue

            svc = get_service(span)
            if svc in EXCLUDE_SERVICES:
                continue

            operation    = normalize_operation(span)
            duration_us  = span.get("duration", 0)
            timestamp_us = span.get("timestamp", 0)
            tags         = span.get("tags", {})
            errors       = classify_errors(span)

            # Walk parent chain to find nearest non-excluded caller
            caller_svc = None
            caller_op  = None
            parent_id  = span.get("parentId")
            visited    = set()

            while parent_id and parent_id not in visited:
                visited.add(parent_id)
                parent = spans_by_id.get(parent_id)
                if parent is None:
                    break
                cs = get_service(parent)
                if cs not in EXCLUDE_SERVICES:
                    caller_svc = cs
                    caller_op  = normalize_operation(parent)
                    break
                parent_id = parent.get("parentId")

            records.append({
                "trace_id":     trace_id,
                "span_id":      span["id"],
                "parent_id":    span.get("parentId"),
                "service":      svc,
                "operation":    operation,
                "caller_svc":   caller_svc,
                "caller_op":    caller_op,
                "duration_us":  duration_us,
                "duration_ms":  round(duration_us / 1000, 3),
                "timestamp_us": timestamp_us,
                "protocol":     "grpc" if tags.get("grpc.path") else "http",
                **errors,
            })

    return records


# ── Downstream fan-out computation ─────────────────────────────────────────

def compute_fanout(traces: list, records: list) -> dict:
    """
    For each (service, operation) pair compute the average number of distinct
    downstream endpoints called per invocation (endpoint fan-out).

    Algorithm:
      1. Build a map: span_id → operation  for SERVER spans.
      2. For each trace, for each SERVER span S in the trace:
           downstream = {operation of SERVER spans whose parentId chain
                         passes through S before reaching another SERVER span}
      3. fan_out[svc][op] = mean(downstream counts per invocation)

    This gives "when this endpoint is called, how many distinct downstream
    endpoints does it fan out to on average?"
    """
    # Map span_id → parsed record (for quick lookups)
    span_to_record: dict = {}
    for r in records:
        span_to_record[r["span_id"]] = r

    # Per-endpoint raw downstream counts (list of counts, one per invocation)
    fanout_raw: dict = defaultdict(list)

    for trace in traces:
        if not trace:
            continue
        spans_by_id = {s["id"]: s for s in trace}

        # Collect all SERVER span ids in this trace
        server_ids = {s["id"] for s in trace if s.get("kind") == "SERVER"
                      and get_service(s) not in EXCLUDE_SERVICES}

        for span in trace:
            if span.get("kind") != "SERVER":
                continue
            svc = get_service(span)
            if svc in EXCLUDE_SERVICES:
                continue
            op  = normalize_operation(span)
            key = (svc, op)

            # BFS / DFS from this span to find all descendant SERVER spans
            # that are direct children (not separated by another SERVER span)
            downstream_ops = set()
            queue = [span["id"]]
            visited = {span["id"]}

            while queue:
                current_id = queue.pop()
                for candidate in trace:
                    if candidate.get("parentId") != current_id:
                        continue
                    cid = candidate["id"]
                    if cid in visited:
                        continue
                    visited.add(cid)
                    if candidate.get("kind") == "SERVER" and cid in server_ids and cid != span["id"]:
                        c_svc = get_service(candidate)
                        c_op  = normalize_operation(candidate)
                        downstream_ops.add((c_svc, c_op))
                        # Don't traverse into the downstream server's sub-tree
                    else:
                        queue.append(cid)

            fanout_raw[key].append(len(downstream_ops))

    # Compute mean fan-out per endpoint
    result = {}
    for (svc, op), counts in fanout_raw.items():
        result[(svc, op)] = round(sum(counts) / len(counts), 4) if counts else 0.0
    return result


# ── Endpoint aggregation ───────────────────────────────────────────────────

def aggregate_endpoints(records: list, fanout: dict) -> list:
    """
    Group span records by (service, operation) and compute:
      - call_count
      - latency distribution: p50, p95, p99, mean
      - error counts & rates: total, 4xx, 5xx, grpc_err, timeout
      - unique callers (for AIS computation used downstream)
      - fan-out (avg downstream endpoints per call)
    """
    groups: dict = defaultdict(list)
    for r in records:
        groups[(r["service"], r["operation"])].append(r)

    agg = []
    for (svc, op), spans in groups.items():
        durations = sorted(s["duration_ms"] for s in spans)
        n         = len(durations)

        def pct(p):
            if n == 0:
                return 0.0
            idx = max(0, int(p / 100 * n) - 1)
            return round(durations[min(idx, n - 1)], 3)

        mean_ms = round(sum(durations) / n, 3) if n else 0.0
        stdev_ms = round(statistics.stdev(durations), 3) if n > 1 else 0.0

        total_errors   = sum(1 for s in spans if s["is_error"])
        count_4xx      = sum(1 for s in spans if s["is_4xx"])
        count_5xx      = sum(1 for s in spans if s["is_5xx"])
        count_grpc_err = sum(1 for s in spans if s["is_grpc_err"])
        count_timeout  = sum(1 for s in spans if s["is_timeout"])

        callers = {(s["caller_svc"], s["caller_op"])
                   for s in spans if s["caller_svc"]}

        fo = fanout.get((svc, op), 0.0)

        agg.append({
            "service":    svc,
            "operation":  op,
            "protocol":   spans[0]["protocol"],
            "call_count": n,
            "latency": {
                "p50_ms":   pct(50),
                "p95_ms":   pct(95),
                "p99_ms":   pct(99),
                "mean_ms":  mean_ms,
                "stdev_ms": stdev_ms,
                "min_ms":   round(durations[0],  3) if durations else 0.0,
                "max_ms":   round(durations[-1], 3) if durations else 0.0,
            },
            "errors": {
                "total":       total_errors,
                "count_4xx":   count_4xx,
                "count_5xx":   count_5xx,
                "count_grpc":  count_grpc_err,
                "count_timeout": count_timeout,
                "rate":        round(total_errors / n, 4) if n else 0.0,
                "rate_4xx":    round(count_4xx  / n, 4) if n else 0.0,
                "rate_5xx":    round(count_5xx  / n, 4) if n else 0.0,
                "rate_grpc":   round(count_grpc_err / n, 4) if n else 0.0,
                "rate_timeout":round(count_timeout / n, 4) if n else 0.0,
            },
            "unique_callers": len(callers),
            "callers": [{"caller_svc": c, "caller_op": o} for c, o in sorted(callers)],
            "fanout_avg": fo,
        })

    agg.sort(key=lambda x: -x["call_count"])
    return agg

def compute_critical_paths(traces: list) -> dict:
    """
    For each trace, reconstruct the DAG and find the critical path —
    the sequence of (service, operation) pairs on the longest wall-clock path.
 
    Design decisions (enforced):
    ─────────────────────────────
    • SERVER spans only — CLIENT spans are mirrors; including them
      double-counts the same call's latency.
    • Root span = no parentId AND earliest timestamp among candidate roots.
      Handles instrumentation noise that produces multiple apparent roots.
    • Effective latency = max(child_end_times) - root_start_time.
      NOT sum(durations) — parallel branches do not stack.
    • Path selection = choose the child branch whose subtree ends latest
      (latest end_time = timestamp + duration), not max duration alone.
    • Deterministic tiebreak = lexicographically smaller spanId wins.
    • Skip trace if: any span has duration=0, missing timestamps,
      or a broken parent chain (parentId present but parent not in trace).
      These produce unreliable DAGs.
 
    Aggregates returned:
    ─────────────────────
    • per_trace        — list of {trace_id, path, latency_ms, skipped, reason}
    • by_path          — path_key → {count, latency_ms_max, latency_ms_mean}
    • by_endpoint      — (service, operation) → appearance_count
    • by_service       — service → appearance_count
    • total_traces     — total traces processed (excluding skipped)
    • total_skipped    — traces skipped due to data quality issues
    """
    per_trace    = []
    path_counts  = {}   # path_key (tuple) → {count, latencies: []}
    ep_counts    = {}   # (svc, op) → int
    svc_counts   = {}   # svc → int
    total_valid  = 0
    total_skip   = 0
 
    for trace in traces:
        if not trace:
            continue
 
        # ── Filter to SERVER spans only ────────────────────────────────────
        server_spans = [s for s in trace if s.get("kind") == "SERVER"
                        and get_service(s) not in EXCLUDE_SERVICES]
        if not server_spans:
            continue
 
        spans_by_id = {s["id"]: s for s in trace}
        server_ids  = {s["id"] for s in server_spans}
 
        # ── Validate: skip if any SERVER span has 0 duration or no timestamp
        skip_reason = None
        for s in server_spans:
            if not s.get("timestamp"):
                skip_reason = "missing_timestamp"
                break
            if s.get("duration", 0) == 0:
                skip_reason = "zero_duration"
                break
 
        if skip_reason:
            per_trace.append({
                "trace_id": trace[0].get("traceId", ""),
                "skipped":  True,
                "reason":   skip_reason,
            })
            total_skip += 1
            continue
 
        # ── Validate: skip if any SERVER span has a parentId that points
        #    to a span not in this trace at all (broken chain)
        for s in server_spans:
            pid = s.get("parentId")
            if pid and pid not in spans_by_id:
                skip_reason = "broken_parent_chain"
                break
 
        if skip_reason:
            per_trace.append({
                "trace_id": trace[0].get("traceId", ""),
                "skipped":  True,
                "reason":   skip_reason,
            })
            total_skip += 1
            continue
 
        # ── Identify root span ─────────────────────────────────────────────
        # Root candidates: SERVER spans whose parentId is absent OR whose
        # parent is not a SERVER span in this trace.
        root_candidates = [
            s for s in server_spans
            if not s.get("parentId") or s.get("parentId") not in server_ids
        ]
        if not root_candidates:
            per_trace.append({
                "trace_id": trace[0].get("traceId", ""),
                "skipped":  True,
                "reason":   "no_root_found",
            })
            total_skip += 1
            continue
 
        # Pick root = earliest timestamp; tiebreak = smallest spanId
        root = min(root_candidates,
                   key=lambda s: (s["timestamp"], s["id"]))
 
        # ── Build children map for SERVER spans only ───────────────────────
        # children[spanId] = list of SERVER child spans
        # A span C is a direct SERVER child of P if:
        #   C's parentId chain reaches P without passing through another
        #   SERVER span in between.
        # We walk the full span tree and assign each SERVER span to its
        # nearest SERVER ancestor.
        children = {s["id"]: [] for s in server_spans}
 
        def nearest_server_ancestor(span):
            """Walk parentId chain; return first SERVER span encountered."""
            pid = span.get("parentId")
            visited = set()
            while pid and pid not in visited:
                visited.add(pid)
                parent = spans_by_id.get(pid)
                if parent is None:
                    return None
                if parent["id"] in server_ids:
                    return parent["id"]
                pid = parent.get("parentId")
            return None
 
        for s in server_spans:
            if s["id"] == root["id"]:
                continue
            ancestor_id = nearest_server_ancestor(s)
            if ancestor_id and ancestor_id in children:
                children[ancestor_id].append(s)
 
        # ── Walk critical path ─────────────────────────────────────────────
        # At each node, choose the child whose subtree ends latest.
        # end_time = timestamp + duration (both in microseconds).
        # Tiebreak: smaller spanId.
 
        def end_time(s):
            return s["timestamp"] + s.get("duration", 0)
 
        def walk_critical_path(span):
            """
            Returns list of (service, operation) from this span downward
            along the latest-ending branch.
            """
            svc = get_service(span)
            op  = normalize_operation(span)
            path = [(svc, op)]
 
            kids = children.get(span["id"], [])
            if not kids:
                return path
 
            # Choose child branch with latest end time; tiebreak = smaller id
            best_child = max(
                kids,
                key=lambda c: (end_time(c), [-ord(ch) for ch in c["id"]])
            )
            # Use negative ord for tiebreak: max() with inverted spanId
            # achieves lexicographically smaller spanId winning.
            # Simpler rewrite:
            best_child = sorted(kids, key=lambda c: (-end_time(c), c["id"]))[0]
 
            path.extend(walk_critical_path(best_child))
            return path
 
        path = walk_critical_path(root)
 
        # ── Effective latency ──────────────────────────────────────────────
        # = max(end_time of all SERVER spans in trace) - root start
        # This correctly handles parallel branches.
        max_end    = max(end_time(s) for s in server_spans)
        latency_us = max_end - root["timestamp"]
        latency_ms = round(latency_us / 1000, 3)
 
        trace_id = trace[0].get("traceId", "")
 
        per_trace.append({
            "trace_id":   trace_id,
            "path":       path,           # [(svc, op), ...]
            "latency_ms": latency_ms,
            "skipped":    False,
        })
        total_valid += 1
 
        # ── Aggregate path frequency ───────────────────────────────────────
        path_key = tuple(f"{s}::{o}" for s, o in path)
        if path_key not in path_counts:
            path_counts[path_key] = {"count": 0, "latencies": []}
        path_counts[path_key]["count"]     += 1
        path_counts[path_key]["latencies"].append(latency_ms)
 
        # ── Aggregate endpoint and service appearance counts ───────────────
        for svc, op in path:
            key = (svc, op)
            ep_counts[key]  = ep_counts.get(key,  0) + 1
            svc_counts[svc] = svc_counts.get(svc, 0) + 1
 
    # ── Build by_path output ───────────────────────────────────────────────
    by_path = []
    for path_key, data in sorted(path_counts.items(),
                                  key=lambda x: -x[1]["count"]):
        lats = data["latencies"]
        by_path.append({
            "path":            list(path_key),
            "count":           data["count"],
            "latency_ms_max":  round(max(lats), 3),
            "latency_ms_mean": round(sum(lats) / len(lats), 3),
        })
 
    # ── Build by_endpoint output (normalised by total_valid) ──────────────
    by_endpoint = [
        {
            "service":           svc,
            "operation":         op,
            "appearance_count":  cnt,
            "appearance_weight": round(cnt / max(total_valid, 1), 6),
        }
        for (svc, op), cnt in sorted(ep_counts.items(), key=lambda x: -x[1])
    ]
 
    # ── Build by_service output ────────────────────────────────────────────
    by_service = [
        {
            "service":           svc,
            "appearance_count":  cnt,
            "appearance_weight": round(cnt / max(total_valid, 1), 6),
        }
        for svc, cnt in sorted(svc_counts.items(), key=lambda x: -x[1])
    ]
 
    return {
        "per_trace":     per_trace,
        "by_path":       by_path,
        "by_endpoint":   by_endpoint,
        "by_service":    by_service,
        "total_traces":  total_valid,
        "total_skipped": total_skip,
    }

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    input_path = UPLOADS_DIR / "zipkin-traces.json"
    print(f"Loading traces from {input_path}…")
    with open(input_path) as f:
        traces = json.load(f)
    print(f"  Loaded {len(traces)} traces")

    print("Parsing SERVER spans…")
    records = parse_traces(traces)
    print(f"  Parsed {len(records)} SERVER spans")

    errors_total = sum(1 for r in records if r["is_error"])
    print(f"  Errors: {errors_total} ({100 * errors_total / max(len(records), 1):.1f}%)")

    print("Computing endpoint fan-out…")
    fanout = compute_fanout(traces, records)

    print("Aggregating per-endpoint metrics…")
    ep_agg = aggregate_endpoints(records, fanout)
    print(f"  {len(ep_agg)} unique endpoints across "
          f"{len({e['service'] for e in ep_agg})} services")

    print("Computing critical paths…")
    cp_results = compute_critical_paths(traces)
    cp_path = OUTPUT_DIR / "critical-paths.json"
    with open(cp_path, "w") as f:
        json.dump(cp_results, f, indent=2)
    print(f"  Saved {cp_path} ({cp_path.stat().st_size:,} bytes)")


    # Save
    spans_path = OUTPUT_DIR / "parsed-spans.json"
    with open(spans_path, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\n  Saved {spans_path} ({spans_path.stat().st_size:,} bytes)")

    agg_path = OUTPUT_DIR / "endpoint-aggregates.json"
    with open(agg_path, "w") as f:
        json.dump(ep_agg, f, indent=2)
    print(f"  Saved {agg_path} ({agg_path.stat().st_size:,} bytes)")

    # Human-readable summary
    print(f"\n  {'Service':<35} {'Endpoint':<45} {'Calls':>6}  {'p50ms':>7}  "
          f"{'p99ms':>7}  {'ErrRate':>8}  {'FanOut':>7}")
    print(f"  {'-'*35} {'-'*45} {'-'*6}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*7}")
    for ep in ep_agg[:20]:
        print(
            f"  {ep['service']:<35} {ep['operation'][:44]:<45} "
            f"{ep['call_count']:>6}  "
            f"{ep['latency']['p50_ms']:>7.1f}  "
            f"{ep['latency']['p99_ms']:>7.1f}  "
            f"{ep['errors']['rate']:>8.4f}  "
            f"{ep['fanout_avg']:>7.2f}"
        )

    return records, ep_agg, traces, cp_results


if __name__ == "__main__":
    main()
