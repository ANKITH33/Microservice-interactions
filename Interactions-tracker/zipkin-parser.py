"""
zipkin_parser.py

Parses raw Zipkin traces into structured span records.

Key design decisions:
  1. Route normalization — collapses /product/OLJCESPC7Z → /product/{id} etc.
     Without this the endpoint graph has hundreds of duplicate nodes.
  2. Only SERVER spans are kept (avoids double-counting with CLIENT mirror).
  3. EXCLUDE_SERVICES filters out infrastructure noise (ingress, loadgen).
  4. Caller is resolved by walking the parentId chain within the same trace,
     skipping excluded services so we get the real application caller.

Input:  zipkin-traces.json  (from uploads or project directory)
Output: outputs/parsed-spans.json
"""

import json
import re
import os
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent

BASE_DIR = CURRENT_DIR.parent / "baseline" / "outputs-baseline"
OUTPUT_DIR = CURRENT_DIR / "outputs"

# Services that are pure infrastructure — exclude as callers/callees
EXCLUDE_SERVICES = {"istio-ingressgateway.istio-system", "loadgenerator.default"}

# Minimum edge calls required for an edge to appear in the graph (see graph builders)
MIN_EDGE_CALLS = 10

# Alphanumeric-only segments that look like product IDs (8-12 uppercase chars)
_PRODUCT_ID_RE = re.compile(r"^[A-Z0-9]{8,12}$")
# Generic numeric IDs
_NUMERIC_ID_RE = re.compile(r"^\d+$")
# UUID-like patterns
_UUID_RE       = re.compile(r"^[0-9a-f]{8,}(-[0-9a-f]{4,}){1,}$", re.I)


def normalize_operation(span: dict) -> str:
    """
    Return a canonical operation name for a span.

    Priority:
      1. grpc.path tag — already fully qualified, e.g. /hipstershop.AdService/GetAds
      2. http.url tag — strip host, normalize IDs in path segments
      3. span name field — last resort
    """
    tags = span.get("tags", {})

    # 1. gRPC path — no normalization needed, already canonical
    grpc_path = tags.get("grpc.path", "")
    if grpc_path:
        return grpc_path

    # 2. HTTP URL
    http_url = tags.get("http.url", "")
    if http_url:
        # Strip scheme+host
        path = re.sub(r"^https?://[^/]+", "", http_url)
        # Drop query string
        path = path.split("?")[0] or "/"
        # Normalize each path segment
        parts = path.split("/")
        normalized = []
        for part in parts:
            if not part:
                normalized.append(part)
            elif _PRODUCT_ID_RE.match(part):
                normalized.append("{id}")
            elif _NUMERIC_ID_RE.match(part):
                normalized.append("{id}")
            elif _UUID_RE.match(part):
                normalized.append("{id}")
            else:
                normalized.append(part)
        return "/".join(normalized) or "/"

    # 3. Span name (Istio sometimes puts the path here)
    name = span.get("name", "unknown")
    # Strip host prefix from Istio-style names like "host:port/path"
    if ":" in name and "/" in name:
        name = name.split("/", 1)[-1]
        name = "/" + name if not name.startswith("/") else name
    return name or "unknown"


def get_service(span: dict) -> str:
    return span.get("localEndpoint", {}).get("serviceName", "unknown")


def parse_traces(traces: list) -> list:
    """
    Convert raw Zipkin traces into flat span records.

    For each SERVER span we:
      - Identify the service it belongs to
      - Normalize the operation name
      - Walk parentId to find the nearest non-excluded caller
      - Record duration, timestamp, error status
    """
    records = []

    for trace in traces:
        if not trace:
            continue

        trace_id    = trace[0].get("traceId", "")
        spans_by_id = {s["id"]: s for s in trace}

        for span in trace:
            # Only process server-side spans to avoid double-counting
            if span.get("kind") != "SERVER":
                continue

            svc = get_service(span)
            if svc in EXCLUDE_SERVICES:
                continue

            operation    = normalize_operation(span)
            duration_us  = span.get("duration", 0)
            timestamp_us = span.get("timestamp", 0)
            tags         = span.get("tags", {})

            # Error detection: HTTP 4xx/5xx or non-zero gRPC status
            status_code  = tags.get("http.status_code", "200")
            grpc_status  = tags.get("grpc.status_code", "0")
            is_error = (
                str(status_code).startswith(("4", "5")) or
                str(grpc_status) not in ("", "0")
            )

            # Resolve caller by walking up the parent chain,
            # skipping excluded services
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
                # Keep walking up if this parent is excluded
                parent_id = parent.get("parentId")

            records.append({
                "trace_id":     trace_id,
                "span_id":      span["id"],
                "service":      svc,
                "operation":    operation,
                "caller_svc":   caller_svc,
                "caller_op":    caller_op,
                "duration_us":  duration_us,
                "duration_ms":  round(duration_us / 1000, 3),
                "timestamp_us": timestamp_us,
                "is_error":     is_error,
                "protocol":     "grpc" if tags.get("grpc.path") else "http",
            })

    return records


def _find_input_file() -> str:
    """Locate zipkin-traces.json — check uploads then project root."""
    candidates = [
        "/mnt/user-data/uploads/zipkin-traces.json",
        os.path.join(BASE_DIR, "zipkin-traces.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "zipkin-traces.json not found. Checked:\n" + "\n".join(candidates)
    )


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    input_file = _find_input_file()
    print(f"Loading traces from {input_file}...")
    with open(input_file) as f:
        traces = json.load(f)
    print(f"  Loaded {len(traces)} traces")

    print("Parsing spans...")
    records = parse_traces(traces)
    print(f"  Parsed {len(records)} SERVER spans")

    services   = set(r["service"]   for r in records)
    operations = set((r["service"], r["operation"]) for r in records)
    errors     = sum(1 for r in records if r["is_error"])
    print(f"  Unique services:   {len(services)}")
    print(f"  Unique operations: {len(operations)}")
    print(f"  Error spans:       {errors} ({100 * errors / max(len(records), 1):.1f}%)")

    # Show normalisation result for frontend to confirm /product/{id} collapsing
    print("\n  Sample normalized operations (frontend):")
    seen = set()
    for r in records:
        if r["service"] == "frontend.default" and r["operation"] not in seen:
            seen.add(r["operation"])
            print(f"    {r['operation']}")

    output_file = os.path.join(OUTPUT_DIR, "parsed-spans.json")
    with open(output_file, "w") as f:
        json.dump(records, f, indent=2)
    print(f"\n  Saved {output_file} ({os.path.getsize(output_file):,} bytes)")


if __name__ == "__main__":
    main()