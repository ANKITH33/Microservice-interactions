# Baseline Script — Google Online Boutique on Minikube + Istio

## Assumptions
- You are in WSL2 (Ubuntu 24.04)
- Docker Desktop is already running on Windows
- You are in the project root directory unless stated otherwise

---

## Step 1 — Start Minikube
**Terminal 1 (WSL)**
```bash
minikube start --cpus=4 --memory=8192 --driver=docker
```

---

## Step 2 — Set istioctl PATH
**Terminal 1**
```bash
export PATH=<PATH_TO_ISTIOCTL_BIN>:$PATH  # e.g. /path/to/istio-1.20.0/bin 
```

Add to `~/.bashrc` to avoid repeating every session:
```bash
echo 'export PATH=<PATH_TO_ISTIOCTL_BIN>:$PATH' >> ~/.bashrc  # replace with your actual path
```

---

## Step 3 — Verify Istio, Zipkin, Prometheus, Grafana
**Terminal 1**
```bash
kubectl get pods -n istio-system
kubectl get svc zipkin prometheus grafana -n istio-system
```
 
Expected: `istiod`, `istio-ingressgateway`, `istio-egressgateway`, `zipkin`, `prometheus`, `grafana` all Running.
 
If **istiod is missing**:
```bash
istioctl install --set profile=demo -y
kubectl label namespace default istio-injection=enabled
```
 
If **zipkin is missing**:
```bash
kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.20/samples/addons/extras/zipkin.yaml -n istio-system
```
 
If **prometheus is missing**:
```bash
kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.20/samples/addons/prometheus.yaml
```
 
If **grafana is missing**:
```bash
kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.20/samples/addons/grafana.yaml
```
 
---
 
## Step 4 — Configure Istio to use Zipkin
**Terminal 1**
```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: istio
  namespace: istio-system
data:
  mesh: |
    defaultConfig:
      tracing:
        zipkin:
          address: zipkin.istio-system:9411
    enableTracing: true
EOF
```
 
---
 
## Step 5 — Enable Prometheus metrics for Istio
**Terminal 1**
 
> Without this, `istio_requests_total` and latency metrics will not appear in Prometheus.
 
```bash
kubectl apply -f - <<EOF
apiVersion: telemetry.istio.io/v1alpha1
kind: Telemetry
metadata:
  name: mesh-default
  namespace: istio-system
spec:
  metrics:
  - providers:
    - name: prometheus
EOF
```
 
Also ensure the Prometheus scrape config has the correct Istio jobs. Check:
```bash
kubectl get configmap prometheus -n istio-system -o jsonpath='{.data.prometheus\.yml}' | grep "job_name"
```
 
Expected jobs: `istio-mesh`, `envoy-stats`, `kubernetes-pods`.
 
If you see only generic Kubernetes jobs (no `istio-mesh` or `envoy-stats`), patch it:
```bash
kubectl patch configmap prometheus -n istio-system --type merge -p '{"data":{"prometheus.yml":"global:\n  scrape_interval: 15s\nscrape_configs:\n- job_name: istio-mesh\n  kubernetes_sd_configs:\n  - role: endpoints\n    namespaces:\n      names:\n      - istio-system\n- job_name: envoy-stats\n  metrics_path: /stats/prometheus\n  kubernetes_sd_configs:\n  - role: pod\n  relabel_configs:\n  - source_labels: [__meta_kubernetes_pod_container_port_name]\n    action: keep\n    regex: .*-envoy-prom\n- job_name: kubernetes-pods\n  kubernetes_sd_configs:\n  - role: pod\n  relabel_configs:\n  - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]\n    action: keep\n    regex: true\n  - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]\n    action: replace\n    target_label: __metrics_path__\n    regex: (.+)\n  - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]\n    action: replace\n    regex: ([^:]+)(?::\\d+)?;(\\d+)\n    replacement: $1:$2\n    target_label: __address__\n- job_name: kubernetes-nodes-cadvisor\n  bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token\n  kubernetes_sd_configs:\n  - role: node\n  relabel_configs:\n  - action: labelmap\n    regex: __meta_kubernetes_node_label_(.+)\n  - replacement: kubernetes.default.svc:443\n    target_label: __address__\n  - regex: (.+)\n    replacement: /api/v1/nodes/$1/proxy/metrics/cadvisor\n    source_labels:\n    - __meta_kubernetes_node_name\n    target_label: __metrics_path__\n  scheme: https\n  tls_config:\n    ca_file: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt\n    insecure_skip_verify: true\n"}}'
kubectl rollout restart deployment/prometheus -n istio-system
```
 
Wait **2 minutes** for Prometheus to restart and scrape.
 
---
 
## Step 6 — Deploy Google Online Boutique
**Terminal 1**
```bash
cd microservices-demo
kubectl apply -f release/kubernetes-manifests.yaml
cd ..
```
 
Wait for all pods to be Running (2–4 minutes):
```bash
kubectl get pods -w
```
 
Expected pods in `default` namespace:
```
adservice, cartservice, checkoutservice, currencyservice, emailservice,
frontend, loadgenerator, paymentservice, productcatalogservice,
recommendationservice, redis-cart, shippingservice
```
 
> **Note:** `emailservice` will be in CrashLoopBackOff — known issue with Python
> gRPC health probe and Istio sidecars. Non-critical, ignore it.
 
> **Note:** `loadgenerator` may briefly show Error during initial startup. Should
> stabilize to 2/2 Running within a few minutes.
 
Verify frontend is reachable:
```bash
kubectl run test --image=curlimages/curl --rm -it --restart=Never \
  --annotations="sidecar.istio.io/inject=false" \
  -- curl -s -o /dev/null -w "%{http_code}" http://frontend:80/
```
Expected: `200`
 
If you get `500`:
```bash
kubectl delete envoyfilter -n istio-system --all
kubectl rollout restart deployment/frontend
```
Then retest.
 
---
 
## Step 7 — Start port-forwards
Open a **separate terminal for each**:
 
**Terminal 2 — Istio ingress tunnel**
```bash
minikube tunnel
```
 
**Terminal 3 — Zipkin**
```bash
kubectl port-forward svc/zipkin 9411:9411 -n istio-system
```
 
**Terminal 4 — Prometheus**
```bash
kubectl port-forward svc/prometheus 9090:9090 -n istio-system
```
 
**Terminal 5 — Grafana**
```bash
kubectl port-forward svc/grafana 3001:3000 -n istio-system
```
 
---
 
## Step 8 — Verify loadgenerator is running
**Terminal 1**
```bash
kubectl get pods | grep loadgenerator
# Should show 2/2 Running
 
kubectl logs deployment/loadgenerator -c main
# Should show locust stats table with requests to /, /cart, /product/... etc.
```
 
If loadgenerator is in Error state:
```bash
kubectl rollout restart deployment/loadgenerator
```
 
Wait **2 minutes** for loadgenerator to stabilize.
 
---
 
## Step 9 — Restart Zipkin to flush old data
**Terminal 1**
 
Restarting Zipkin clears all stored traces (Zipkin v2.23 does not support flush via API).
```bash
kubectl rollout restart deployment/zipkin -n istio-system
kubectl wait --for=condition=available deployment/zipkin -n istio-system --timeout=60s
```
 
Wait **10 minutes** for fresh traces to accumulate.
 
Verify all services are present:
```bash
curl "http://localhost:9411/api/v2/services"
```
Expected: `frontend.default`, `cartservice.default`, `checkoutservice.default`, etc. (10+ services)
 
Verify Prometheus has Istio metrics:
```bash
curl "http://localhost:9090/api/v1/label/__name__/values" | python3 -m json.tool | grep "istio_requests"
```
Expected: `"istio_requests_total"` in output.
 
---
 
## Step 10 — Run baseline collector
**Terminal 1**
```bash
pip3 install requests --break-system-packages
python3 baseline/collect_baseline.py
```
 
This dumps the following into `baseline/outputs-baseline/`:
- `zipkin-services.json` — list of all services
- `zipkin-traces.json` — all traces from the last 10 minutes
- `zipkin-dependencies.json` — dependency graph
- `prometheus-metrics.json` — point-in-time metrics (request rate, p50/p95/p99 latency, error rate)
- `prometheus-metrics-range.json` — 10 minute time-series for the same metrics
- `collection-summary.json` — overview of what was collected
---
 
## Step 11 — Verify outputs
**Terminal 1**
```bash
ls -lh baseline/outputs-baseline/
```
 
- `zipkin-traces.json` should be at least a few hundred KB
- `zipkin-dependencies.json` should have 10+ dependency links
- `prometheus-metrics.json` should be several MB
---
 
