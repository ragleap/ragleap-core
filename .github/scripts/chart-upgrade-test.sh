#!/usr/bin/env bash
# Upgrade test for the ragleap-ops chart: install a PUBLISHED version from PyPI, write data, upgrade in place to the
# chart in this checkout, then check that the databases were not restarted and that the data survived.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1 (the shared VPS has too little disk).
# Env: FROM_VERSION (published ragleap-ops version to start from, default 0.5.2), GHCR_TOKEN and GITHUB_ACTOR (optional:
# with them the app and voice pods run for real after the upgrade; without them they stay scaled to zero and the
# summary says NOT EXERCISED).
set -euo pipefail

CL=chart-upgrade-test
NS=ops-upgrade-test
FROM_VERSION="${FROM_VERSION:-0.5.2}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
NEW=$ROOT/packages/ragleap-ops/helm/ragleap-ops
OLD=/tmp/ops-old-chart
FAILS=0
RESULTS=()
APPS_ON=0

stop() { echo "STOP: $*"; exit 1; }
record() { RESULTS+=("$1"); echo "$1"; }
fail() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }

if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi
[[ "$FROM_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || stop "FROM_VERSION must look like 0.5.2 (got '$FROM_VERSION')"

diag() {
  echo "== diagnostics"
  kubectl -n "$NS" get pods,pvc -o wide 2>&1 | tail -25 || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -20 || true
  helm -n "$NS" history ops 2>&1 | tail -5 || true
}
cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ] || [ "$FAILS" -ne 0 ]; then diag; fi
  kill $(jobs -p) 2>/dev/null || true
  kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  echo "cleanup done (kind cluster $CL deleted)"
}
trap cleanup EXIT

echo "== preflight"
for c in kubectl helm curl python3; do command -v "$c" >/dev/null || stop "$c not found"; done
if ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.24.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.24.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
CLUSTERS=$(kind get clusters 2>&1 || true)
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi
[ -f "$NEW/Chart.yaml" ] || stop "chart not found at $NEW"

echo "== getting the published chart $FROM_VERSION from PyPI"
rm -rf "$OLD" /tmp/ops-old-wheel && mkdir -p "$OLD" /tmp/ops-old-wheel
python3 -m pip download "ragleap-ops==$FROM_VERSION" --no-deps --no-cache-dir -q -d /tmp/ops-old-wheel
WHL=$(ls /tmp/ops-old-wheel/*.whl)
python3 -m zipfile -e "$WHL" "$OLD"
OLDCHART=$OLD/ragleap_ops/helm/ragleap-ops
[ -f "$OLDCHART/Chart.yaml" ] || stop "no chart inside the $FROM_VERSION wheel (wheels before 0.5.1 did not contain it)"
echo "upgrading from chart $(sed -n 's/^version: //p' "$OLDCHART/Chart.yaml") (published) to chart $(sed -n 's/^version: //p' "$NEW/Chart.yaml") (this checkout)"

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
kubectl create namespace "$NS"

pod_uid() { # name prefix
  kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.uid}{"\n"}{end}' | awk -v p="$1" 'index($1,p)==1 {print $2; exit}'
}
pvc_vol() { # claim name
  kubectl -n "$NS" get pvc "$1" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true
}
need_running() { # deployment name: at least one replica configured, and every configured replica ready
  local i spec ready
  for i in $(seq 1 60); do
    spec=$(kubectl -n "$NS" get deploy "$1" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
    ready=$(kubectl -n "$NS" get deploy "$1" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
    spec=${spec:-0}; ready=${ready:-0}
    if [ "$spec" -ge 1 ] && [ "$ready" = "$spec" ]; then break; fi
    sleep 5
  done
  if [ "$spec" -ge 1 ] && [ "$ready" = "$spec" ]; then
    record "PASS  $1 has $ready of $spec replicas ready after the upgrade"
  else
    fail "$1 has $ready of $spec replicas ready (want at least 1, all ready)"
  fi
}

echo "== phase 1: install the published chart (app and voice at zero)"
helm install ops "$OLDCHART" -n "$NS" --set app.replicaCount=0 --set voice.replicaCount=0 --wait --timeout 600s \
  && record "PASS  installed the published chart $FROM_VERSION" || { fail "install of the published chart did not become ready"; exit 1; }
kubectl -n "$NS" get pods,pvc

echo "== phase 2: write data and record what must not change"
kubectl -n "$NS" exec deploy/ragleap-db -c db -- psql -U ragleap -d ragleap_core -v ON_ERROR_STOP=1 \
  -c "CREATE TABLE upgrade_probe (id int, note text);" \
  -c "INSERT INTO upgrade_probe VALUES (1, 'postgres-upgrade-row');" \
  -c "CHECKPOINT;" && record "PASS  wrote a probe row to Postgres" || { fail "could not write to Postgres"; exit 1; }
kubectl -n "$NS" exec deploy/ragleap-neo4j -c neo4j -- cypher-shell -u neo4j -p ragleapgraph \
  "CREATE (:UpgradeProbe {note: 'neo4j-upgrade-node'});" && record "PASS  wrote a probe node to Neo4j" || { fail "could not write to Neo4j"; exit 1; }
DB_UID_BEFORE=$(pod_uid ragleap-db-); NEO_UID_BEFORE=$(pod_uid ragleap-neo4j-)
DB_VOL_BEFORE=$(pvc_vol ragleap-db-data); NEO_VOL_BEFORE=$(pvc_vol ragleap-neo4j-data)
echo "before: db pod $DB_UID_BEFORE, neo4j pod $NEO_UID_BEFORE, db volume $DB_VOL_BEFORE, neo4j volume $NEO_VOL_BEFORE"
[ -n "$DB_UID_BEFORE" ] && [ -n "$NEO_UID_BEFORE" ] && [ -n "$DB_VOL_BEFORE" ] && [ -n "$NEO_VOL_BEFORE" ] || { fail "could not read the database pods or volumes before the upgrade"; exit 1; }

UP_ARGS=(--set app.replicaCount=0 --set voice.replicaCount=0)
if [ -n "${GHCR_TOKEN:-}" ]; then
  [ -n "${GITHUB_ACTOR:-}" ] || stop "GHCR_TOKEN is set but GITHUB_ACTOR is not"
  command -v docker >/dev/null || stop "GHCR_TOKEN is set but docker was not found"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin >/dev/null
  DOCKER_CFG="$HOME/.docker/config.json"
  [ -f "$DOCKER_CFG" ] || stop "docker login did not write $DOCKER_CFG"
  grep -q '"auth"' "$DOCKER_CFG" || stop "the Docker config has no inline auth, so a pull secret cannot be built from it"
  kubectl -n "$NS" create secret generic ghcr-pull-secret --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson="$DOCKER_CFG"
  kubectl -n "$NS" create secret generic ragleap-app-env --from-literal=EXPERIMENT_PLACEHOLDER=1
  APPS_ON=1
  UP_ARGS=()
else
  record "NOT EXERCISED  app and voice pods after the upgrade (GHCR_TOKEN not set, so they stay scaled to zero)"
fi

echo "== phase 3: helm upgrade to the chart in this checkout"
if helm upgrade ops "$NEW" -n "$NS" ${UP_ARGS[@]+"${UP_ARGS[@]}"} --wait --timeout 600s; then
  record "PASS  helm upgrade to the chart in this checkout succeeded"
else
  fail "helm upgrade failed"; exit 1
fi
kubectl -n "$NS" get pods,pvc

echo "== checks"
REV=$(helm list -n "$NS" -o json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['revision'] + ' ' + d[0]['status'])")
if [ "$REV" = "2 deployed" ]; then record "PASS  the release is at revision 2, deployed"; else fail "release revision/status is '$REV' (want '2 deployed')"; fi

DB_UID_AFTER=$(pod_uid ragleap-db-); NEO_UID_AFTER=$(pod_uid ragleap-neo4j-)
if [ "$DB_UID_BEFORE" = "$DB_UID_AFTER" ]; then record "PASS  the Postgres pod was not restarted by the upgrade"; else fail "the Postgres pod was replaced during the upgrade ($DB_UID_BEFORE -> $DB_UID_AFTER)"; fi
if [ "$NEO_UID_BEFORE" = "$NEO_UID_AFTER" ]; then record "PASS  the Neo4j pod was not restarted by the upgrade"; else fail "the Neo4j pod was replaced during the upgrade ($NEO_UID_BEFORE -> $NEO_UID_AFTER)"; fi

DB_VOL_AFTER=$(pvc_vol ragleap-db-data); NEO_VOL_AFTER=$(pvc_vol ragleap-neo4j-data)
if [ "$DB_VOL_BEFORE" = "$DB_VOL_AFTER" ]; then record "PASS  the Postgres volume is the same volume ($DB_VOL_AFTER)"; else fail "the Postgres volume changed ($DB_VOL_BEFORE -> '$DB_VOL_AFTER')"; fi
if [ "$NEO_VOL_BEFORE" = "$NEO_VOL_AFTER" ]; then record "PASS  the Neo4j volume is the same volume ($NEO_VOL_AFTER)"; else fail "the Neo4j volume changed ($NEO_VOL_BEFORE -> '$NEO_VOL_AFTER')"; fi

ROW=""
for i in 1 2 3 4 5 6; do
  ROW=$(kubectl -n "$NS" exec deploy/ragleap-db -c db -- psql -U ragleap -d ragleap_core -At -c "SELECT note FROM upgrade_probe WHERE id = 1;" 2>/dev/null || true)
  [ "$ROW" = "postgres-upgrade-row" ] && break
  sleep 10
done
echo "postgres says: [$ROW]"
if [ "$ROW" = "postgres-upgrade-row" ]; then record "PASS  the Postgres probe row survived the upgrade"; else fail "the Postgres probe row is gone after the upgrade (got: $ROW)"; fi

NODE=""
for i in 1 2 3 4 5 6; do
  NODE=$(kubectl -n "$NS" exec deploy/ragleap-neo4j -c neo4j -- cypher-shell -u neo4j -p ragleapgraph --format plain "MATCH (n:UpgradeProbe) RETURN n.note;" 2>/dev/null || true)
  case "$NODE" in *neo4j-upgrade-node*) break ;; esac
  sleep 10
done
echo "neo4j says: [$NODE]"
case "$NODE" in *neo4j-upgrade-node*) record "PASS  the Neo4j probe node survived the upgrade" ;; *) fail "the Neo4j probe node is gone after the upgrade (got: $NODE)" ;; esac

if [ "$APPS_ON" = "1" ]; then
  APP_SPEC=$(kubectl -n "$NS" get deploy ragleap-app -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
  VOICE_SPEC=$(kubectl -n "$NS" get deploy ragleap-voice -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
  record "OBSERVED  right after the plain helm upgrade (no --set): ragleap-app spec.replicas=${APP_SPEC:-none}, ragleap-voice spec.replicas=${VOICE_SPEC:-none}"
  record "OBSERVED  computed values: $(helm get values ops -n "$NS" -a -o json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print("app.replicaCount=%s voice.replicaCount=%s" % (d["app"]["replicaCount"], d["voice"]["replicaCount"]))' 2>/dev/null || echo unreadable)"
  if [ "${APP_SPEC:-0}" != "1" ] || [ "${VOICE_SPEC:-0}" != "1" ]; then
    echo "app and voice are not at one replica after a plain upgrade; upgrading again with explicit replica counts"
    if helm upgrade ops "$NEW" -n "$NS" --set app.replicaCount=1 --set voice.replicaCount=1 --wait --timeout 600s; then
      record "PASS  second helm upgrade with explicit replica counts succeeded"
    else
      fail "second helm upgrade with explicit replica counts failed"
    fi
  fi
  need_running ragleap-app
  need_running ragleap-voice
fi

echo
echo "== SUMMARY ($(date +%T))"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo "Not exercised: upgrades that change values, downgrades and rollbacks, other kubernetes versions, multi-node scheduling, major database version changes,"
echo "application behaviour (the app and voice readiness check is a TCP port check)."
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
