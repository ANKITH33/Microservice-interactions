"""
dashboard/app.py

Flask backend for the microservice analysis dashboard.
Serves all 8 JSON outputs and provides a single-page HTML dashboard.

Usage:
    cd <project_root>
    python3 dashboard/app.py

Expects outputs/ directory to be at ../outputs/ relative to this file.
"""

import json
import os
from pathlib import Path
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

OUTPUTS_DIR = Path(__file__).resolve().parent.parent / "Interactions-tracker" / "outputs"

FILES = [
    "bottlenecks.json",
    "metrics-service.json",
    "metrics-endpoint.json",
    "critical-paths.json",
    "graph-service.json",
    "graph-endpoint.json",
    "endpoint-aggregates.json",
    "parsed-spans.json",
    "prometheus-processed.json",
]


def _load(filename: str):
    path = OUTPUTS_DIR / filename
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/all")
def all_data():
    data = {}
    for fname in FILES:
        key = fname.replace(".json", "").replace("-", "_")
        data[key] = _load(fname)
    return jsonify(data)


@app.route("/api/<filename>")
def get_file(filename):
    if not filename.endswith(".json"):
        filename += ".json"
    data = _load(filename)
    if data is None:
        return jsonify({"error": f"{filename} not found"}), 404
    return jsonify(data)


if __name__ == "__main__":
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    print(f"Outputs dir: {OUTPUTS_DIR}")
    print("Starting dashboard at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
