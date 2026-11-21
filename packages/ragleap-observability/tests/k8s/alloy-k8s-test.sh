#!/usr/bin/env bash
# Kubernetes test of the optional Alloy log shipper of ragleap-observability, on a throwaway kind cluster.
# Checks: (1) the chart refuses Alloy and Promtail enabled together, (2) with Alloy on and Promtail off, lines written by
# a test pod arrive in Loki with the namespace, pod and container labels, (3) no Promtail runs next to Alloy,
# (4) Alloy log lines with level=error are counted (information only).
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1 (the shared VPS has no room for a kind cluster).
set -euo pipefail

CL=alloy-k8s-test
NS=alloy-test
GEN=alloy-gen
REL=alloy
AG=ragleap-observability-agents
HERE=$(cd "$(dirname "$0")" && pwd)
CH=$HERE/../../helm/ragleap-observability
LOKI_PORT=13100
FAILS=0
RESULTS=()

stop() { echo "STOP: $*"; exit 1; }
record() { RESULTS+=("$1"); echo "$1"; }
pass() { record "PASS  $1"; }
fail() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }

if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi

cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ] || [ "$FAILS" -ne 0 ]; then
    echo "== diagnostics (exit code $rc)"
    kubectl -n "$NS" get pods -o wide 2>&1 | tail -20 || true
    kubectl -n "$AG" get pods -o wide 2>&1 | tail -20 || true
    kubectl -n "$AG" logs ds/ragleap-alloy --tail=60 2>&1 || true
    kubectl -n "$NS" logs deploy/ragleap-loki --tail=30 2>&1 || true
  fi
  kill $(jobs -p) 2>/dev/null || true
  kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  echo "cleanup done (kind cluster $CL deleted)"
}
trap cleanup EXIT

echo "== preflight"
for c in kubectl helm curl python3; do command -v "$c" >/dev/null || stop "$c not found"; done
[ -f "$CH/Chart.yaml" ] || stop "chart not found at $CH"
echo "chart under test: $(sed -n 's/^version: //p' "$CH/Chart.yaml"), Alloy image: $(sed -n '/^alloy:/,/^[a-z]/s/^  image: //p' "$CH/values.yaml" | head -n 1)"

echo "== chart rendering (no cluster needed)"
OUT=$(helm template g "$CH" --set alloy.enabled=true --set promtail.enabled=true 2>&1 || true)
if grep -q "alloy.enabled and promtail.enabled are both true" <<< "$OUT"; then
  pass "the chart refuses Alloy and Promtail enabled together"
else
  fail "the chart did not refuse Alloy and Promtail enabled together"
fi
if helm template g "$CH" --set alloy.enabled=true --set promtail.enabled=false 2>&1 | grep -q "name: ragleap-alloy$"; then
  pass "with Promtail off, the chart renders the Alloy DaemonSet"
else
  fail "with Promtail off, the chart did not render the Alloy DaemonSet"
fi

if ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.24.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.24.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
kind version
CLUSTERS=$(kind get clusters 2>&1 || true)
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi

echo "== creating kind cluster $CL"
kind create cluster --name "$CL" --wait 180s
kubectl create namespace "$NS"

echo "== installing the chart: Loki and Alloy only (Promtail, Grafana, Prometheus, AlertManager, postgres-exporter off)"
helm install "$REL" "$CH" -n "$NS" \
  --set promtail.enabled=false --set alloy.enabled=true \
  --set grafana.enabled=false --set prometheus.enabled=false \
  --set alertmanager.enabled=false --set postgresExporter.enabled=false \
  --wait --timeout 300s
kubectl -n "$NS" get pods
kubectl -n "$AG" get pods

kubectl -n "$NS" port-forward svc/ragleap-loki "$LOKI_PORT:3100" >/dev/null 2>&1 &
LOKI_UP=no
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:$LOKI_PORT/ready"; then LOKI_UP=yes; break; fi
  sleep 1
done
[ "$LOKI_UP" = yes ] || stop "Loki port-forward failed"

echo "== a test pod writes one marker line per second"
MARK="alloy-marker-$(date +%s)-$RANDOM"
kubectl create namespace "$GEN"
kubectl -n "$GEN" apply -f - <<EOY
apiVersion: v1
kind: Pod
metadata:
  name: marker-gen
spec:
  containers:
    - name: gen
      image: busybox:1.36
      command: ["sh", "-c", "i=0; while true; do echo \"$MARK \$i\"; i=\$((i+1)); sleep 1; done"]
EOY
kubectl -n "$GEN" wait --for=condition=Ready pod/marker-gen --timeout=180s

lq() { # prints "count|namespace|pod|container" of the marker lines found in Loki
  curl -s -G "http://127.0.0.1:$LOKI_PORT/loki/api/v1/query_range" \
    --data-urlencode "query={namespace=\"$GEN\"} |= \"$MARK\"" \
    --data-urlencode "limit=20" \
    --data-urlencode "start=$(( $(date +%s) - 900 ))000000000" \
  | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
except Exception:
    r = []
n = sum(len(s['values']) for s in r)
l = r[0]['stream'] if r else {}
print('%d|%s|%s|%s' % (n, l.get('namespace', ''), l.get('pod', ''), l.get('container', '')))
"
}

echo "== waiting up to 5 minutes for the marker in Loki"
GOT="0|||"
for i in $(seq 1 60); do
  GOT=$(lq || echo "0|||")
  CNT=${GOT%%|*}
  if [ "${CNT:-0}" -ge 1 ] 2>/dev/null; then break; fi
  sleep 5
done
IFS='|' read -r CNT LNS LPOD LCON <<< "$GOT"
if [ "${CNT:-0}" -ge 1 ] 2>/dev/null; then pass "a line written by a test pod reached Loki through Alloy ($CNT lines)"; else fail "no marker line reached Loki through Alloy within 5 minutes"; fi
if [ "$LNS" = "$GEN" ]; then pass "label namespace is $GEN"; else fail "label namespace is '$LNS', want $GEN"; fi
if [ "$LPOD" = "marker-gen" ]; then pass "label pod is marker-gen"; else fail "label pod is '$LPOD', want marker-gen"; fi
if [ "$LCON" = "gen" ]; then pass "label container is gen"; else fail "label container is '$LCON', want gen"; fi

echo "== Alloy DaemonSet state"
READY=$(kubectl -n "$AG" get ds ragleap-alloy -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}' 2>&1 || true)
if [ "$READY" = "1/1" ]; then pass "the Alloy DaemonSet is ready (1/1)"; else fail "the Alloy DaemonSet is not ready ($READY)"; fi
DSLIST=$(kubectl -n "$AG" get ds -o name 2>&1 || true)
if grep -q "ragleap-promtail" <<< "$DSLIST"; then fail "a Promtail DaemonSet runs next to Alloy"; else pass "no Promtail DaemonSet runs next to Alloy"; fi
ERRS=$(kubectl -n "$AG" logs ds/ragleap-alloy --tail=500 2>&1 | grep -c 'level=error' || true)
record "INFO  Alloy log lines with level=error: $ERRS (not counted as a failure)"

echo
echo "== summary"
for r in "${RESULTS[@]}"; do echo "$r"; done
if [ "$FAILS" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILS CHECK(S) FAILED"
  exit 1
fi
