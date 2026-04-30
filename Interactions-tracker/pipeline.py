"""
pipeline.py

Runs the full analysis pipeline end-to-end:

  Step 1  zipkin_parser          — parse raw traces, normalize routes
  Step 2  prometheus_collector   — extract per-service Prometheus metrics
  Step 3  graph_service_builder  — build service dependency graph
  Step 4  graph_endpoint_builder — build endpoint dependency graph
  Step 5  metrics_calculator     — compute bottleneck/risk/cohesion scores
  Step 6  bottleneck_detector    — rank and classify bottlenecks

All intermediate outputs land in ./outputs/

Usage:
    python3 pipeline.py
"""

import os
import sys
import importlib
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

STEPS = [
    ("zipkin-parser",          "Step 1 — Parse Zipkin traces & normalize routes"),
    ("prometheus-collector",   "Step 2 — Extract Prometheus metrics"),
    ("graph-service-builder",  "Step 3 — Build service dependency graph"),
    ("graph-endpoint-builder", "Step 4 — Build endpoint dependency graph"),
    ("metrics-calculator",     "Step 5 — Compute bottleneck, risk & cohesion scores"),
    ("bottleneck-detector",    "Step 6 — Detect & rank bottlenecks"),
]


def _bar(char: str = "═", width: int = 70) -> str:
    return char * width


def run():
    print(_bar())
    print("  Microservice Analysis Pipeline")
    print(_bar())

    total_start = time.time()

    for module_name, description in STEPS:
        print(f"\n{_bar('-')}")
        print(f"  {description}")
        print(_bar("-"))

        step_start = time.time()
        try:
            mod = importlib.import_module(module_name)
            # Reload if already imported (allows re-running pipeline in same session)
            importlib.reload(mod)
            mod.main()
        except Exception as exc:
            print(f"\n  ✗ ERROR in {module_name}: {exc}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

        elapsed = time.time() - step_start
        print(f"\n  ✓ Completed in {elapsed:.1f}s")

    total_elapsed = time.time() - total_start
    print(f"\n{_bar()}")
    print(f"  Pipeline complete in {total_elapsed:.1f}s")
    print(f"  Outputs written to: {os.path.join(BASE_DIR, 'outputs')}/")
    print(_bar())

    # List output files
    output_dir = os.path.join(BASE_DIR, "outputs")
    if os.path.isdir(output_dir):
        print("\n  Output files:")
        for fname in sorted(os.listdir(output_dir)):
            fpath = os.path.join(output_dir, fname)
            size  = os.path.getsize(fpath)
            print(f"    {fname:<35} {size:>10,} bytes")


if __name__ == "__main__":
    run()