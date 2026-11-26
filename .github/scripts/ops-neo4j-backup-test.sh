#!/usr/bin/env bash
# Test of the neo4j backup cronjob (packages/ragleap-ops/k8s/neo4j-backup-cronjob.yaml) on a throwaway kind cluster.
# It applies the real manifests, writes a marker row to Neo4j and triggers the cronjob twice:
#   leg 1: a normal backup. Neo4j must go to 0 replicas and come back to 1, a non-empty neo4j.dump must exist
#          in the backup volume and the marker must still be there after Neo4j restarts.
#   leg 2: a backup whose dump fails on purpose (dump of a database that does not exist). Neo4j must still
#          come back to 1 replica. This is the gap the scale-up-watcher sidecar was added to close.
# Not covered: restoring a dump, a cluster with more than one node, the Helm chart (the cronjob is not in it).
# Optional KUBECTL_IMAGE (for example alpine/kubectl:1.37.2) replaces both kubectl images in a temp copy of the
# manifest, so a candidate image can be tested without changing the file.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1.
set -euo pipefail

CL=ops-neo4j-backup-test
NS=ragleap-core
CJ=ragleap-neo4j-backup
EXPECTED=11
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
K8S=$ROOT/packages/ragleap-ops/k8s
WORK=$(mktemp -d)
PASSES=0
FAILS=0
RESULTS=()
POLLPID=""

stop() { echo "STOP: $*"; exit 1; }
record() { RESULTS+=("$1"); echo "$1"; }
ok() { PASSES=$((PASSES + 1)); record "PASS  $1"; }
bad() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }

if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi

poll_stop() {
  if [ -n "$POLLPID" ]; then
    kill "$POLLPID" 2>/dev/null || true
    wait "$POLLPID" 2>/dev/null || true
    POLLPID=""
  fi
}

cleanup() {
  poll_stop
  kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  echo "cleanup done (kind cluster $CL deleted)"
}
trap cleanup EXIT

diag() {
  echo "== diagnostics"
  kubectl -n "$NS" get pods,jobs -o wide 2>&1 | tail -30 || true
  kubectl -n "$NS" get events --sort-by=.lastTimestamp 2>&1 | tail -25 || true
  local p
  for p in $(kubectl -n "$NS" get pods --field-selector=status.phase!=Running,status.phase!=Succeeded -o name 2>/dev/null | head -5); do
    echo "-- describe $p"
    kubectl -n "$NS" describe "$p" 2>&1 | tail -30 || true
  done
}

count_in() { # file fixed-string -> number of occurrences
  { grep -F -o -- "$2" "$1" || true; } | wc -l | tr -d '[:space:]'
}

echo "== preflight"
[ -n "${KIND_NODE_IMAGE:-}" ] || stop "KIND_NODE_IMAGE is not set"
for c in kubectl curl awk; do command -v "$c" >/dev/null || stop "$c not found"; done
if ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.33.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
kind version
CLUSTERS=$(kind get clusters 2>&1 || true)
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi
for f in namespace serviceaccounts neo4j-pvc backup-pvc neo4j-secret neo4j-deployment neo4j-backup-cronjob; do
  [ -f "$K8S/$f.yaml" ] || stop "$K8S/$f.yaml not found"
done

CRON=$WORK/cronjob.yaml
FAILCRON=$WORK/cronjob-faildump.yaml
cp "$K8S/neo4j-backup-cronjob.yaml" "$CRON"
if [ -n "${KUBECTL_IMAGE:-}" ]; then
  [[ "$KUBECTL_IMAGE" =~ ^alpine/kubectl:[0-9A-Za-z._-]+$ ]] || stop "KUBECTL_IMAGE must look like alpine/kubectl:1.2.3"
  [ "$(count_in "$CRON" "image: alpine/kubectl:")" = "2" ] || stop "expected exactly 2 kubectl image lines in the cronjob"
  sed -i -E "s#image: alpine/kubectl:[0-9A-Za-z._-]+#image: ${KUBECTL_IMAGE}#" "$CRON"
  [ "$(count_in "$CRON" "image: ${KUBECTL_IMAGE}")" = "2" ] || stop "image override did not apply to both containers"
fi
echo "kubectl image lines under test:"
grep -n 'image: alpine/kubectl' "$CRON"
[ "$(count_in "$CRON" 'database dump neo4j --to-path')" = "1" ] || stop "dump command anchor does not match once"
sed 's#database dump neo4j --to-path#database dump doesnotexist --to-path#' "$CRON" > "$FAILCRON"
[ "$(count_in "$FAILCRON" 'database dump doesnotexist --to-path')" = "1" ] || stop "failing variant was not produced"

AUTH=$(sed -n 's/^  NEO4J_AUTH: *//p' "$K8S/neo4j-secret.yaml")
[ -n "$AUTH" ] || stop "NEO4J_AUTH not found in neo4j-secret.yaml"
NEO_USER=${AUTH%%/*}
NEO_PW=${AUTH#*/}

echo "== cluster"
kind create cluster --name "$CL" --image "$KIND_NODE_IMAGE" --wait 180s
K8S_VER=$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')
echo "kubernetes $K8S_VER"

cypher() { kubectl -n "$NS" exec deploy/ragleap-neo4j -- cypher-shell -u "$NEO_USER" -p "$NEO_PW" "$1"; }
cypher_retry() {
  local i out=""
  for i in $(seq 1 30); do
    if out=$(cypher "$1" 2>&1); then printf '%s\n' "$out"; return 0; fi
    sleep 5
  done
  printf '%s\n' "$out"
  return 1
}
marker_count() { cypher_retry "MATCH (n:BackupTestMarker) RETURN count(n) AS c" | tail -1 | tr -d '[:space:]'; }

poll_start() { # logfile: one line per second, "<epoch> <spec.replicas>"
  : > "$1"
  (
    while :; do
      r=$(kubectl -n "$NS" get deploy ragleap-neo4j -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
      echo "$(date +%s) ${r:-?}" >> "$1"
      sleep 1
    done
  ) &
  POLLPID=$!
}
history() { awk '{ if ($2 != p) { print "  " $0; p = $2 } }' "$1"; }
saw_zero() { awk '$2 == "0" { n++ } END { exit !n }' "$1"; }
saw_cycle() { awk '$2 == "0" { z = 1 } z && $2 == "1" { ok = 1 } END { exit !ok }' "$1"; }
neo4j_available() { kubectl -n "$NS" wait --for=jsonpath='{.status.availableReplicas}'=1 deploy/ragleap-neo4j --timeout=300s; }
job_logs() { kubectl -n "$NS" logs "job/$1" --all-containers --prefix --tail=40 2>&1 || true; }

echo "== deploy neo4j from the real manifests"
for f in namespace serviceaccounts neo4j-pvc backup-pvc neo4j-secret neo4j-deployment; do
  kubectl apply -f "$K8S/$f.yaml"
done
if kubectl -n "$NS" rollout status deploy/ragleap-neo4j --timeout=420s; then
  ok "neo4j deployment ready"
else
  bad "neo4j deployment did not become ready"; diag; stop "cannot test the backup without a running neo4j"
fi
kubectl apply -f "$CRON"

cypher_retry "CREATE (:BackupTestMarker {id: 'ops-neo4j-backup-test'})" > /dev/null || true
M=$(marker_count || true)
if [ "$M" = "1" ]; then ok "marker row written and readable"; else bad "marker row not readable (got '${M}')"; diag; stop "no data to back up"; fi

echo "== leg 1: normal backup"
poll_start "$WORK/replicas-ok.log"
kubectl -n "$NS" create job bk-ok --from=cronjob/"$CJ"
if kubectl -n "$NS" wait --for=condition=complete job/bk-ok --timeout=600s; then
  ok "leg 1: backup job completed"
else
  bad "leg 1: backup job did not complete"; diag
fi
poll_stop
echo "replica history (leg 1):"; history "$WORK/replicas-ok.log"
if saw_zero "$WORK/replicas-ok.log"; then ok "leg 1: neo4j was scaled to 0 for the dump"; else bad "leg 1: neo4j was never seen at 0 replicas"; fi
if neo4j_available; then ok "leg 1: neo4j is back to 1 available replica"; else bad "leg 1: neo4j did not come back"; diag; fi
echo "job logs (leg 1):"; job_logs bk-ok

kubectl -n "$NS" delete pod backup-check --ignore-not-found >/dev/null
kubectl -n "$NS" apply -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: backup-check
  namespace: ragleap-core
spec:
  restartPolicy: Never
  containers:
    - name: c
      image: busybox:1.36
      command: ["sh", "-c", "ls -l /backups; find /backups -name neo4j.dump -size +0c"]
      volumeMounts:
        - name: b
          mountPath: /backups
  volumes:
    - name: b
      persistentVolumeClaim:
        claimName: ragleap-backup-data
YAML
kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/backup-check --timeout=180s || true
kubectl -n "$NS" logs backup-check || true
D=$( { kubectl -n "$NS" logs backup-check 2>/dev/null | grep '/neo4j.dump$' || true; } | wc -l | tr -d '[:space:]')
if [ "$D" -ge 1 ]; then ok "leg 1: a non-empty neo4j.dump exists in the backup volume"; else bad "leg 1: no non-empty neo4j.dump in the backup volume"; fi
M=$(marker_count || true)
if [ "$M" = "1" ]; then ok "leg 1: marker row survived the restart"; else bad "leg 1: marker row missing after the restart (got '${M}')"; fi

echo "== leg 2: the dump fails on purpose"
kubectl -n "$NS" delete job bk-ok --ignore-not-found --wait=true >/dev/null
kubectl apply -f "$FAILCRON"
poll_start "$WORK/replicas-fail.log"
kubectl -n "$NS" create job bk-fail --from=cronjob/"$CJ"
CYCLE=0
for i in $(seq 1 120); do
  if saw_cycle "$WORK/replicas-fail.log"; then CYCLE=1; break; fi
  sleep 3
done
poll_stop
echo "replica history (leg 2):"; history "$WORK/replicas-fail.log"
if [ "$CYCLE" = "1" ]; then ok "leg 2: neo4j went to 0 and was scaled back to 1 after a failed dump"; else bad "leg 2: neo4j was not scaled back up after a failed dump"; fi
if neo4j_available; then ok "leg 2: neo4j is 1 available replica after the failed dump"; else bad "leg 2: neo4j is not available after the failed dump"; diag; fi

dump_failed() {
  local s f c
  s=$(kubectl -n "$NS" get pods -l job-name=bk-fail -o jsonpath='{range .items[*].status.containerStatuses[?(@.name=="dump")]}{.restartCount}|{.state.terminated.exitCode}|{.lastState.terminated.exitCode}{"\n"}{end}' 2>/dev/null || true)
  if [ -n "$s" ] && awk -F'|' '($1 + 0) > 0 || ($2 != "" && $2 != "0") || ($3 != "" && $3 != "0") { f = 1 } END { exit !f }' <<< "$s"; then return 0; fi
  c=$(kubectl -n "$NS" get job bk-fail -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null || true)
  [ "$c" = "True" ]
}
FAILED=0
for i in $(seq 1 80); do
  if dump_failed; then FAILED=1; break; fi
  sleep 3
done
if [ "$FAILED" = "1" ]; then ok "leg 2: the dump really failed (the leg is valid)"; else bad "leg 2: no sign that the dump failed, so leg 2 proves nothing"; fi
echo "job logs (leg 2):"; job_logs bk-fail
M=$(marker_count || true)
if [ "$M" = "1" ]; then ok "leg 2: marker row intact after the failed dump"; else bad "leg 2: marker row missing after the failed dump (got '${M}')"; fi

poll_stop
echo
echo "== summary (kubernetes $K8S_VER)"
printf '%s\n' "${RESULTS[@]}"
echo "checks passed: $PASSES of $EXPECTED, failed: $FAILS"
if [ "$FAILS" -ne 0 ] || [ "$PASSES" -ne "$EXPECTED" ]; then
  echo "VERDICT: FAIL"
  exit 1
fi
echo "VERDICT: PASS"
