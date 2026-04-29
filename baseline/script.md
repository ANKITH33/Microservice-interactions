# Baseline Script — Google Online Boutique on Minikube + Istio

## Assumptions
- You are in WSL2 (Ubuntu 24.04)
- Docker Desktop is already running on Windows
- All commands run in WSL terminals unless stated otherwise

---

## Step 1 — Start Minikube
**Terminal 1 (WSL)**
```bash
minikube start --cpus=4 --memory=8192 --driver=docker
```

---

## Step 2 — Set istioctl PATH
**Terminal 1 (WSL)**
```bash
export PATH=/mnt/c/Users/ankit/Desktop/Microservice-interactions/istio-1.20.0/bin:$PATH
```

Add to `~/.bashrc` to avoid repeating:
```bash
echo 'export PATH=/mnt/c/Users/ankit/Desktop/Microservice-interactions/istio-1.20.0/bin:$PATH' >> ~/.bashrc
```

---

## Step 3 — Verify Istio
**Terminal 1 (WSL)**
```bash
kubectl get pods -n istio-system
```

Expected: `istiod`, `istio-ingressgateway`, `istio-egressgateway` all Running.

If **istiod is missing**, install Istio:
```bash
istioctl install --set profile=demo -y
kubectl label namespace default istio-injection=enabled
```

---

## Step 4 — Verify Zipkin
```bash
kubectl get svc zipkin -n istio-system
```

If **not found**, deploy it:
```bash
kubectl apply -f https://raw.githubusercontent.com/istio/istio/release-1.20/samples/addons/extras/zipkin.yaml -n istio-system
```

---

## Step 5 — Verify Prometheus + Grafana
```bash
kubectl get svc prometheus grafana -n istio-system
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

## Step 6 — Configure Istio to use Zipkin for tracing
**Terminal 1 (WSL)**
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

## Step 7 — Deploy Google Online Boutique
**Terminal 1 (WSL)**
```bash
cd /mnt/c/Users/ankit/Desktop/Microservice-interactions/
git clone https://github.com/GoogleCloudPlatform/microservices-demo
cd microservices-demo
kubectl apply -f release/kubernetes-manifests.yaml
```

Wait for all pods to be Running (takes 2–4 minutes):
```bash
kubectl get pods -w
```

Expected pods in `default` namespace:
```
adservice
cartservice
checkoutservice
currencyservice
emailservice
frontend
loadgenerator
paymentservice
productcatalogservice
recommendationservice
shippingservice
redis-cart
```

---

## Step 8 — Deploy KMamiz (if not already deployed)
**Terminal 1 (WSL)**
```bash
cd /mnt/c/Users/ankit/Desktop/Microservice-interactions/
kubectl apply -f KMamiz/deploy/kmamiz-rbac.yaml
kubectl apply -f KMamiz/deploy/kmamiz-demo-mongodb.yaml
kubectl apply -f KMamiz/deploy/kmamiz-sample.yaml
kubectl apply -f KMamiz/envoy/EnvoyFilter-WASM.yaml -n istio-system
```

---

## Step 9 — Verify everything is running
**Terminal 1 (WSL)**
```bash
kubectl get pods -A
```

All pods in `default`, `istio-system`, and `kmamiz-system` should be Running.

---

## Step 10 — Start port-forwards
Open a **separate terminal for each**:

**Terminal 2 — Istio ingress tunnel**
```bash
minikube tunnel
```
Keep this running. Enter sudo password if prompted.

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

**Terminal 6 — KMamiz**
```bash
kubectl port-forward svc/kmamiz -n kmamiz-system 8888:80
```

---

## Step 11 — Verify access
Open in browser:
- Zipkin: http://localhost:9411
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3001
- KMamiz: http://localhost:8888
- Online Boutique frontend: http://localhost/  (via minikube tunnel)

---

## Step 12 — Generate load
Online Boutique includes a built-in `loadgenerator` pod that runs automatically.
Verify it's working:
```bash
kubectl logs deployment/loadgenerator
```

You should see requests being made. Wait 5–10 minutes for enough traces to accumulate in Zipkin and KMamiz.

To also send manual load:
```bash
# Terminal 1 (WSL)
for i in {1..300}; do
  curl -s http://localhost/ > /dev/null
  curl -s http://localhost/product/OLJCESPC7Z > /dev/null
  curl -s http://localhost/cart > /dev/null
  sleep 0.2
done
```

---

## Step 13 — Save baseline data

**Zipkin traces:**
```bash
curl "http://localhost:9411/api/v2/traces?limit=500" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/zipkin-traces.json
```

**Prometheus metrics:**
```bash
# Request rate
curl "http://localhost:9090/api/v1/query?query=rate(istio_requests_total[5m])" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/prom-request-rate.json

# p99 latency
curl "http://localhost:9090/api/v1/query?query=histogram_quantile(0.99,rate(istio_request_duration_milliseconds_bucket[5m]))" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/prom-p99-latency.json

# Error rate
curl "http://localhost:9090/api/v1/query?query=rate(istio_requests_total{response_code!~\"2..\"}[5m])" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/prom-error-rate.json

# CPU usage
curl "http://localhost:9090/api/v1/query?query=rate(container_cpu_usage_seconds_total[5m])" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/prom-cpu.json

# Memory usage
curl "http://localhost:9090/api/v1/query?query=container_memory_usage_bytes" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/prom-memory.json
```

**KMamiz outputs:**
```bash
# Service dependency graph
curl "http://localhost:8888/api/v1/graph/service" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/kmamiz-service-graph.json

# Endpoint dependency graph
curl "http://localhost:8888/api/v1/graph/endpoint" \
  > /mnt/c/Users/ankit/Desktop/Microservice-interactions/baseline/kmamiz-endpoint-graph.json
```

---

## Step 14 — Screenshots to take (outputs-baseline/)
- KMamiz: dependency graph, insights page, metrics page
- Grafana: Istio Service Dashboard, Istio Workload Dashboard
- Zipkin: trace list, one full trace waterfall (pick a `frontend` trace)

---

## Notes
- Online Boutique's `loadgenerator` sends continuous traffic — no need to keep manual curl loops running
- If KMamiz graph is empty, wait 5 more minutes and refresh — it needs enough traces to build the graph
- Traces in Zipkin are queryable by service name e.g. `frontend`, `checkoutservice`, etc.