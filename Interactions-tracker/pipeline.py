#!/usr/bin/env python3
"""
KMamiz Pipeline — single file version
Usage: python3 pipeline.py --zipkin http://localhost:9411 --lookback 15
"""
import argparse, json, math, logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

# ── Zipkin collector ──────────────────────────────────────────────────────────

def fetch_traces(zipkin_url, lookback_minutes=15, limit=1000):
    end_ts = int(datetime.utcnow().timestamp() * 1_000_000)
    params = {"endTs": end_ts, "lookback": lookback_minutes * 60 * 1000, "limit": limit}
    try:
        r = httpx.get(f"{zipkin_url}/api/v2/traces", params=params, timeout=30)
        r.raise_for_status()
        traces = r.json()
        log.info(f"Fetched {len(traces)} traces, {sum(len(t) for t in traces)} spans")
        return traces
    except Exception as e:
        log.error(f"Zipkin error: {e}")
        return []

# ── Dependency graph ──────────────────────────────────────────────────────────

def build_graph(traces):
    services   = {}   # name -> {endpoints, is_gateway}
    calls      = []   # (caller_svc, callee_svc)
    svc_deps   = defaultdict(set)

    span_index = {}
    for trace in traces:
        for span in trace:
            span_index[span["id"]] = span

    edge_set = set()

    for trace in traces:
        for span in trace:
            svc = span.get("localEndpoint", {}).get("serviceName", "unknown")
            if svc == "unknown": continue

            if svc not in services:
                services[svc] = {"endpoints": set(), "is_gateway": "gw" in svc or "gateway" in svc}

            ep = _endpoint(span)
            if ep:
                services[svc]["endpoints"].add(ep)

            parent_id = span.get("parentId")
            if not parent_id: continue
            parent = span_index.get(parent_id)
            if not parent: continue

            caller_svc = parent.get("localEndpoint", {}).get("serviceName", "unknown")
            if caller_svc == "unknown" or caller_svc == svc: continue

            edge = (caller_svc, svc)
            if edge not in edge_set:
                edge_set.add(edge)
                calls.append(edge)
                svc_deps[caller_svc].add(svc)

    return services, calls, svc_deps

def _endpoint(span):
    tags = span.get("tags", {})
    method = tags.get("http.method")
    path = tags.get("http.url") or tags.get("http.path")
    if not method or not path:
        parts = span.get("name", "").split(" ", 1)
        if len(parts) == 2:
            method, path = parts
    if not method or not path: return None
    # strip host from URL
    import re
    path = re.sub(r"https?://[^/]+", "", path) or "/"
    return f"{method.upper()} {path}"

# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_coupling(services, svc_deps):
    callers_of = defaultdict(set)
    for caller, callees in svc_deps.items():
        for callee in callees:
            callers_of[callee].add(caller)

    result = {}
    for svc, info in services.items():
        ais = len(callers_of[svc]) + (1 if info["is_gateway"] else 0)
        ads = len(svc_deps.get(svc, set()))
        result[svc] = {"AIS": ais, "ADS": ads, "ACS": ais * ads}
    return result

def compute_cohesion(services, calls):
    # consumer -> {callee -> set of endpoints used}
    usage = defaultdict(lambda: defaultdict(set))
    for (caller, callee) in calls:
        usage[caller][callee]  # just register the relationship

    # For SIUC we need endpoint-level calls — approximate from span data
    result = {}
    for svc, info in services.items():
        endpoints = list(info["endpoints"])
        sidc = _sidc(endpoints)
        consumers = [c for c, deps in usage.items() if svc in deps]
        siuc = None if not consumers else 1.0  # approximation without ep-level call data
        tsic = sidc if siuc is None else (sidc + siuc) / 2
        result[svc] = {
            "SIDC": round(sidc, 4) if sidc is not None else None,
            "SIUC": siuc,
            "TSIC": round(tsic, 4) if tsic is not None else None,
        }
    return result

def _sidc(endpoints):
    if len(endpoints) < 2: return 1.0 if endpoints else None
    vecs = [_vec(ep) for ep in endpoints]
    vocab = set(k for v in vecs for k in v)
    pairs, total = 0, 0.0
    for i in range(len(vecs)):
        for j in range(i+1, len(vecs)):
            total += _cos(vecs[i], vecs[j], vocab)
            pairs += 1
    return total / pairs if pairs else 1.0

def _vec(ep):
    parts = ep.split(" ", 1)
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    v = {f"M:{method}": 1}
    for seg in path.strip("/").split("/"):
        if seg and not seg.startswith("{"):
            v[f"S:{seg}"] = v.get(f"S:{seg}", 0) + 1
    return v

def _cos(v1, v2, vocab):
    dot = sum(v1.get(t,0)*v2.get(t,0) for t in vocab)
    m1 = math.sqrt(sum(v1.get(t,0)**2 for t in vocab))
    m2 = math.sqrt(sum(v2.get(t,0)**2 for t in vocab))
    return dot/(m1*m2) if m1 and m2 else 0.0

def compute_latency(traces):
    durs = defaultdict(list)
    errs = defaultdict(int)
    for trace in traces:
        for span in trace:
            svc = span.get("localEndpoint", {}).get("serviceName", "unknown")
            ep = _endpoint(span)
            if not ep or svc == "unknown": continue
            key = f"{svc} | {ep}"
            durs[key].append(span.get("duration", 0) / 1000.0)
            if span.get("tags", {}).get("http.status_code", "200") >= "400":
                errs[key] += 1

    result = {}
    for key, ds in durs.items():
        ds.sort()
        n = len(ds)
        mean = sum(ds)/n
        std = math.sqrt(sum((d-mean)**2 for d in ds)/n)
        result[key] = {
            "p50_ms": round(_pct(ds, 50), 2),
            "p95_ms": round(_pct(ds, 95), 2),
            "p99_ms": round(_pct(ds, 99), 2),
            "mean_ms": round(mean, 2),
            "cv": round(std/mean if mean else 0, 4),
            "requests": n,
            "errors": errs.get(key, 0),
        }
    return result

def _pct(s, p):
    if not s: return 0.0
    idx = (p/100)*(len(s)-1)
    lo, hi = int(idx), min(int(idx)+1, len(s)-1)
    return s[lo] + (s[hi]-s[lo])*(idx-lo)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zipkin",   default="http://localhost:9411")
    parser.add_argument("--lookback", default=15, type=int)
    parser.add_argument("--output",   default="output")
    args = parser.parse_args()

    Path(args.output).mkdir(exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    traces = fetch_traces(args.zipkin, args.lookback)
    services, calls, svc_deps = build_graph(traces)
    coupling  = compute_coupling(services, svc_deps)
    cohesion  = compute_cohesion(services, calls)
    latency   = compute_latency(traces)

    out = {
        "timestamp": ts,
        "summary": {"services": len(services), "traces": len(traces)},
        "service_dependency_graph": {k: sorted(v) for k,v in svc_deps.items()},
        "coupling_metrics": coupling,
        "cohesion_metrics": cohesion,
        "endpoint_latency": latency,
    }

    out_path = f"{args.output}/metrics_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    log.info(f"Saved to {out_path}")

    # ── Print summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Results — {ts}")
    print(f"{'='*60}")
    print(f"  Services: {len(services)}   Traces: {len(traces)}")

    print(f"\n  {'Service':<30} {'AIS':>4} {'ADS':>4} {'ACS':>4}")
    print(f"  {'-'*44}")
    for svc, m in sorted(coupling.items()):
        print(f"  {svc:<30} {m['AIS']:>4} {m['ADS']:>4} {m['ACS']:>4}")

    print(f"\n  {'Service':<30} {'SIDC':>6} {'TSIC':>6}")
    print(f"  {'-'*44}")
    for svc, m in sorted(cohesion.items()):
        sidc = f"{m['SIDC']:.3f}" if m['SIDC'] is not None else "  N/A"
        tsic = f"{m['TSIC']:.3f}" if m['TSIC'] is not None else "  N/A"
        print(f"  {svc:<30} {sidc:>6} {tsic:>6}")

    print(f"\n  Top endpoints by p99 latency:")
    print(f"  {'Endpoint':<50} {'p99ms':>7} {'reqs':>6}")
    print(f"  {'-'*65}")
    for ep, lat in sorted(latency.items(), key=lambda x: -x[1]['p99_ms'])[:10]:
        print(f"  {ep[:49]:<50} {lat['p99_ms']:>7.1f} {lat['requests']:>6}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()