#!/usr/bin/env bash
# Install, upgrade and uninstall test for the three RagLeap Helm charts on a throwaway kind cluster.
# Plain helm (not 'ct install'), so no files are added to the charts and no chart version has to change.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1, because the shared VPS
# does not have the disk and memory for a kind cluster.
set -euo pipefail

CL=chart-install-test
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OPS=$ROOT/packages/ragleap-ops/helm/ragleap-ops
APP=$ROOT/packages/ragleap-app-chart/helm/ragleap-app-chart
OBS=$ROOT/packages/ragleap-observability/helm/ragleap-observability
FAILS=0
RESULTS=()

stop() { echo "STOP: $*"; exit 1; }

if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi

cleanup() {
  kill $(jobs -p) 2>/dev/null || true
  kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  echo "cleanup done (kind cluster $CL deleted)"
}
trap cleanup EXIT

record() { RESULTS+=("$1"); echo "$1"; }
fail() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }

diag() { # namespace
  echo "== diagnostics for namespace $1"
  kubectl -n "$1" get pods -o wide 2>&1 | tail -25 || true
  kubectl -n "$1" get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
  local p
  for p in $(kubectl -n "$1" get pods --field-selector=status.phase!=Running,status.phase!=Succeeded -o name 2>/dev/null | head -5); do
    echo "-- describe $p"
    kubectl -n "$1" describe "$p" 2>&1 | tail -25 || true
  done
}

echo "== preflight"
for c in kubectl helm curl; do command -v "$c" >/dev/null || stop "$c not found"; done
if ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.24.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.24.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
kind version
CLUSTERS=$(kind get clusters 2>&1 || true)
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi
for d in "$OPS" "$APP" "$OBS"; do [ -f "$d/Chart.yaml" ] || stop "chart not found at $d"; done
echo "ops $(sed -n 's/^version: //p' "$OPS/Chart.yaml"), app-chart $(sed -n 's/^version: //p' "$APP/Chart.yaml"), observability $(sed -n 's/^version: //p' "$OBS/Chart.yaml")"

# values-example.yaml starts with a byte order mark; strip it so the test does not depend on how helm treats it
sed '1s/^\xEF\xBB\xBF//' "$APP/values-example.yaml" > /tmp/app-values-example.yaml

echo "== creating kind cluster $CL"
KIND_ARGS=()
if [ -n "${KIND_NODE_IMAGE:-}" ]; then
  case "$KIND_NODE_IMAGE" in
    kindest/node:v1.*@sha256:*) ;;
    *) stop "KIND_NODE_IMAGE must look like kindest/node:v1.xx.y@sha256:... (got '$KIND_NODE_IMAGE')" ;;
  esac
  KIND_ARGS=(--image "$KIND_NODE_IMAGE")
fi
kind create cluster --name "$CL" --wait 180s ${KIND_ARGS[@]+"${KIND_ARGS[@]}"}
SERVER_V=$(kubectl version -o json | python3 -c 'import sys,json; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')
record "OBSERVED  kubernetes server version: $SERVER_V"
if [ -n "${KIND_NODE_IMAGE:-}" ]; then
  WANT_V=${KIND_NODE_IMAGE#kindest/node:}; WANT_V=${WANT_V%%@*}
  if [ "${SERVER_V%%+*}" = "$WANT_V" ]; then record "PASS  the cluster runs the requested Kubernetes version $WANT_V"; else fail "requested Kubernetes $WANT_V but the cluster reports $SERVER_V"; fi
fi

need_deploy() { # label namespace name
  if kubectl -n "$2" wait --for=condition=Available "deployment/$3" --timeout=180s >/dev/null 2>&1; then
    record "PASS  $1: deployment $3 exists and is available"
  else
    fail "$1: deployment $3 is missing or not available"; diag "$2"
  fi
}
need_daemonset() { # label namespace name
  local desired="0" ready="0" i
  for i in $(seq 1 24); do
    desired=$(kubectl -n "$2" get daemonset "$3" -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || true)
    ready=$(kubectl -n "$2" get daemonset "$3" -o jsonpath='{.status.numberReady}' 2>/dev/null || true)
    desired=${desired:-0}; ready=${ready:-0}
    if [ "$desired" -ge 1 ] && [ "$ready" = "$desired" ]; then break; fi
    sleep 5
  done
  if [ "$desired" -ge 1 ] && [ "$ready" = "$desired" ]; then
    record "PASS  $1: daemonset $3 in $2 has $ready of $desired pods ready"
  else
    fail "$1: daemonset $3 in $2 has $ready of $desired pods ready (want at least 1, all ready)"; diag "$2"
  fi
}
gone() { # label kind name [namespace]
  local i args=()
  if [ -n "${4:-}" ]; then args=(-n "$4"); fi
  for i in $(seq 1 18); do
    if ! kubectl get "$2" "$3" "${args[@]}" >/dev/null 2>&1; then record "PASS  $1: $2 $3 is gone after uninstall"; return 0; fi
    sleep 5
  done
  fail "$1: $2 $3 still exists 90 seconds after uninstall"
}
post_install() { # label namespace
  case "$1" in
    ragleap-ops)
      need_deploy "$1" "$2" ragleap-db
      need_deploy "$1" "$2" ragleap-neo4j
      if [ "$APPS_ON" = "1" ]; then
        need_deploy "$1" "$2" ragleap-app
        need_deploy "$1" "$2" ragleap-voice
      fi ;;
    ragleap-app-chart)
      need_deploy "$1" "$2" app-api
      need_deploy "$1" "$2" app-cache ;;
    ragleap-observability)
      need_deploy "$1" "$2" ragleap-prometheus
      need_deploy "$1" "$2" ragleap-grafana
      need_deploy "$1" "$2" ragleap-loki
      need_deploy "$1" "$2" ragleap-alertmanager
      need_deploy "$1" "$2" ragleap-postgres-exporter
      need_daemonset "$1" ragleap-observability-agents ragleap-promtail ;;
  esac
}
post_uninstall() { # label
  case "$1" in
    ragleap-observability)
      gone "$1" namespace ragleap-observability-agents
      gone "$1" clusterrole ragleap-promtail-role
      gone "$1" clusterrolebinding ragleap-promtail-binding ;;
  esac
}

chart_test() { # label chart-dir namespace release keep-pattern -- helm args
  local label=$1 dir=$2 ns=$3 rel=$4 keep=$5
  shift 6
  echo
  echo "== $label: template, install, upgrade, uninstall ($(date +%T))"
  if helm template "$rel" "$dir" -n "$ns" "$@" >/dev/null 2>/tmp/tpl.err; then record "PASS  $label: helm template renders"; else fail "$label: helm template failed: $(head -c 300 /tmp/tpl.err)"; return 0; fi

  if helm install "$rel" "$dir" -n "$ns" "$@" --wait --timeout 600s; then
    record "PASS  $label: install (all workloads ready)"
  else
    fail "$label: install did not become ready"; diag "$ns"
    helm uninstall "$rel" -n "$ns" --wait --timeout 300s >/dev/null 2>&1 || true
    return 0
  fi
  kubectl -n "$ns" get pods
  post_install "$label" "$ns"

  if helm upgrade "$rel" "$dir" -n "$ns" "$@" --wait --timeout 600s; then record "PASS  $label: upgrade with the same values"; else fail "$label: upgrade failed"; diag "$ns"; fi

  if helm uninstall "$rel" -n "$ns" --wait --timeout 300s; then record "PASS  $label: uninstall"; else fail "$label: uninstall failed"; diag "$ns"; fi
  local left
  left=$(kubectl -n "$ns" get deploy,statefulset,daemonset,pvc -o name 2>/dev/null | grep -v -x -e "$keep" || true)
  if [ -z "$left" ]; then record "PASS  $label: nothing left behind (deployments, statefulsets, daemonsets, PVCs)"; else fail "$label: left behind after uninstall: $(tr '\n' ' ' <<< "$left")"; fi
  post_uninstall "$label"
}

kubectl create namespace ops-test
kubectl create namespace app-test
kubectl create namespace obs-test

# ragleap-ops: the app and voice pods use the private ghcr.io/ragleap/ragleap-app image. With GHCR_TOKEN they run for real
# (pull secret built from the Docker config file, placeholder ragleap-app-env Secret); without it they are scaled to zero
# and the summary says so. Readiness of these pods is a TCP check: it shows the image starts and listens, not that the app works.
APPS_ON=0
OPS_ARGS=(--set app.replicaCount=0 --set voice.replicaCount=0)
if [ -n "${GHCR_TOKEN:-}" ]; then
  [ -n "${GITHUB_ACTOR:-}" ] || stop "GHCR_TOKEN is set but GITHUB_ACTOR is not"
  command -v docker >/dev/null || stop "GHCR_TOKEN is set but docker was not found"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin >/dev/null
  DOCKER_CFG="$HOME/.docker/config.json"
  [ -f "$DOCKER_CFG" ] || stop "docker login did not write $DOCKER_CFG"
  grep -q '"auth"' "$DOCKER_CFG" || stop "the Docker config has no inline auth, so a pull secret cannot be built from it"
  kubectl -n ops-test create secret generic ghcr-pull-secret --type=kubernetes.io/dockerconfigjson \
    --from-file=.dockerconfigjson="$DOCKER_CFG"
  kubectl -n ops-test create secret generic ragleap-app-env --from-literal=EXPERIMENT_PLACEHOLDER=1
  APPS_ON=1
  OPS_ARGS=()
else
  record "NOT EXERCISED  ragleap-ops: app and voice pods (GHCR_TOKEN not set, so they are scaled to zero)"
fi
chart_test "ragleap-ops" "$OPS" ops-test ops "none-to-keep" -- ${OPS_ARGS[@]+"${OPS_ARGS[@]}"}

# ragleap-app-chart: its own worked example (public images: hashicorp/http-echo and redis).
chart_test "ragleap-app-chart" "$APP" app-test app "none-to-keep" -- \
  -f /tmp/app-values-example.yaml

# ragleap-observability: defaults (Prometheus, Grafana, Loki, Promtail, AlertManager, postgres-exporter),
# plus a stand-in Postgres and the exporter Secret so the exporter has something to connect to.
kubectl -n obs-test apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ragleap-db
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ragleap-db
  template:
    metadata:
      labels:
        app: ragleap-db
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: POSTGRES_PASSWORD
              value: testpw
          ports:
            - containerPort: 5432
---
apiVersion: v1
kind: Service
metadata:
  name: ragleap-db
spec:
  selector:
    app: ragleap-db
  ports:
    - port: 5432
      targetPort: 5432
EOF
kubectl -n obs-test create secret generic ragleap-db-exporter-secret \
  --from-literal=connection-string='postgresql://postgres:testpw@ragleap-db:5432/postgres?sslmode=disable'
kubectl -n obs-test rollout status deploy/ragleap-db --timeout=240s
chart_test "ragleap-observability" "$OBS" obs-test obs "deployment.apps/ragleap-db" -- \
  --set grafana.admin.password=ci-throwaway-password

echo
echo "== SUMMARY ($(date +%T))"
for r in "${RESULTS[@]}"; do echo "$r"; done
if [ "$APPS_ON" = "1" ]; then
  APPNOTE="what the app and voice pods do beyond starting (TCP readiness, placeholder config only)"
else
  APPNOTE="the ragleap-ops app and voice pods (no GHCR_TOKEN)"
fi
echo "Not exercised: ct install itself (values from ci/ folders), $APPNOTE,"
echo "ingress and TLS, other values files (dev, staging, prod), upgrades that change values, multi-node behaviour."
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
