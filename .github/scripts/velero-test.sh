#!/usr/bin/env bash
# Backup and restore test: Velero (file-system backup via the node agent) with a RustFS S3 stand-in, on a throwaway
# kind cluster. Workload: the ragleap-ops chart's Postgres and Neo4j with data written before the backup.
# Steps: install, write data, back up the namespace, delete the namespace, restore, verify the data came back.
# Meant for a GitHub runner. It refuses to run elsewhere unless FORCE=1.
# Versions come from the environment (the workflow passes them): VELERO_VERSION, AWS_PLUGIN_VERSION, S3_IMAGE.
set -euo pipefail

CL=velero-test
NS=ragleap-ops-test
VELERO_VERSION="${VELERO_VERSION:-v1.18.3}"
AWS_PLUGIN_VERSION="${AWS_PLUGIN_VERSION:-v1.14.0}"
S3_IMAGE="${S3_IMAGE:-rustfs/rustfs@sha256:9ca9559533b537d10482fe923288b676ad08f92a7069d9a17327fbae3cab688b}"
CLUSTER="${CLUSTER:-kind}"
STORAGE="${STORAGE:-static}"
K3D_VERSION="${K3D_VERSION:-v5.9.0}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.34.12-k3s1}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OPS=$ROOT/packages/ragleap-ops/helm/ragleap-ops
ACCESS_KEY="velero$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SECRET_KEY="secret$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
FAILS=0
RESULTS=()

stop() { echo "STOP: $*"; exit 1; }
record() { RESULTS+=("$1"); echo "$1"; }
fail() { FAILS=$((FAILS + 1)); record "FAIL  $1"; }

if [ "${GITHUB_ACTIONS:-}" != "true" ] && [ "${FORCE:-}" != "1" ]; then
  stop "not on a GitHub runner. A kind cluster needs several GB of disk and memory. Set FORCE=1 to run anyway."
fi

diag() {
  echo "== diagnostics"
  kubectl get pods -A 2>&1 | tail -30 || true
  kubectl -n velero get backups,restores,podvolumebackups,podvolumerestores,backupstoragelocations 2>&1 | tail -30 || true
  kubectl -n velero logs deploy/velero --tail=60 2>&1 || true
  kubectl -n velero logs ds/node-agent --tail=40 2>&1 || true
  kubectl -n "$NS" get pods,pvc 2>&1 | tail -20 || true
  kubectl -n "$NS" describe pods 2>&1 | grep -E "Warning|Error|Failed|restore-wait" | head -20 || true
  velero backup logs probe-backup 2>&1 | tail -30 || true
  velero restore logs probe-restore 2>&1 | tail -30 || true
}
cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ] || [ "$FAILS" -ne 0 ]; then diag; fi
  kill $(jobs -p) 2>/dev/null || true
  if [ "$CLUSTER" = "k3d" ]; then
    k3d cluster delete "$CL" >/dev/null 2>&1 || true
  else
    kind delete cluster --name "$CL" >/dev/null 2>&1 || true
  fi
  echo "cleanup done ($CLUSTER cluster $CL deleted)"
}
trap cleanup EXIT

echo "== preflight (Velero $VELERO_VERSION, AWS plugin $AWS_PLUGIN_VERSION, S3 stand-in $S3_IMAGE)"
for c in kubectl helm curl tar; do command -v "$c" >/dev/null || stop "$c not found"; done
if [ "$CLUSTER" = "kind" ] && ! command -v kind >/dev/null; then
  echo "kind not preinstalled; downloading v0.24.0"
  curl -sLo /tmp/kind https://github.com/kubernetes-sigs/kind/releases/download/v0.24.0/kind-linux-amd64
  chmod +x /tmp/kind
  export PATH=/tmp:$PATH
fi
case "$CLUSTER" in
  kind) kind version ;;
  k3d)
    mkdir -p /tmp/k3d-bin
    curl -fsSL -o /tmp/k3d-bin/k3d "https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION/k3d-linux-amd64" || stop "could not download k3d $K3D_VERSION"
    chmod +x /tmp/k3d-bin/k3d
    export PATH=/tmp/k3d-bin:$PATH
    echo "k3d binary sha256 $(sha256sum /tmp/k3d-bin/k3d | cut -d' ' -f1)"
    k3d version
    echo "k3s image: $K3S_IMAGE" ;;
  *) stop "CLUSTER must be kind or k3d (got '$CLUSTER')" ;;
esac
case "$STORAGE" in
  static) ;;
  default) [ "$CLUSTER" = "k3d" ] || stop "STORAGE=default needs CLUSTER=k3d: kind's default storage creates hostPath volumes, which Velero file-system backup skips" ;;
  *) stop "STORAGE must be static or default (got '$STORAGE')" ;;
esac
if [ "$CLUSTER" = "k3d" ]; then
  CLUSTERS=$(k3d cluster list --no-headers 2>&1 | awk '{print $1}' || true)
else
  CLUSTERS=$(kind get clusters 2>&1 || true)
fi
if grep -q -x -e "$CL" <<< "$CLUSTERS"; then stop "kind cluster $CL already exists"; fi
[ -f "$OPS/Chart.yaml" ] || stop "ops chart not found at $OPS"

echo "== downloading the Velero CLI $VELERO_VERSION"
mkdir -p /tmp/velero-cli
GOT=""
for owner in velero-io vmware-tanzu; do
  URL="https://github.com/$owner/velero/releases/download/$VELERO_VERSION/velero-$VELERO_VERSION-linux-amd64.tar.gz"
  if curl -fsSL -o /tmp/velero-cli/velero.tgz "$URL"; then GOT=$owner; break; fi
done
[ -n "$GOT" ] || stop "could not download the Velero CLI $VELERO_VERSION"
echo "downloaded from github.com/$GOT; sha256 $(sha256sum /tmp/velero-cli/velero.tgz | cut -d' ' -f1)"
tar -xzf /tmp/velero-cli/velero.tgz -C /tmp/velero-cli --strip-components=1
export PATH=/tmp/velero-cli:$PATH
velero version --client-only

echo "== creating kind cluster $CL"
if [ "$CLUSTER" = "k3d" ]; then
  k3d cluster create "$CL" --image "$K3S_IMAGE" --k3s-arg "--disable=traefik@server:0" --wait --timeout 180s
else
  kind create cluster --name "$CL" --wait 180s
fi
kubectl get nodes -o wide
kubectl get storageclass

echo "== what does the default storage class create? (throwaway PVC and pod in the default namespace)"
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: probe-default
  namespace: default
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: probe-default
  namespace: default
spec:
  containers:
    - name: c
      image: busybox:1.36
      command: ["sleep", "300"]
      volumeMounts:
        - name: v
          mountPath: /v
  volumes:
    - name: v
      persistentVolumeClaim:
        claimName: probe-default
EOF
if kubectl -n default wait --for=condition=Ready pod/probe-default --timeout=180s >/dev/null 2>&1; then
  PVN=$(kubectl -n default get pvc probe-default -o jsonpath='{.spec.volumeName}')
  DSC=$(kubectl -n default get pvc probe-default -o jsonpath='{.spec.storageClassName}')
  DPROV=$(kubectl get storageclass "$DSC" -o jsonpath='{.provisioner}')
  HP=$(kubectl get pv "$PVN" -o jsonpath='{.spec.hostPath.path}' 2>/dev/null || true)
  LP=$(kubectl get pv "$PVN" -o jsonpath='{.spec.local.path}' 2>/dev/null || true)
  record "OBSERVED  default storage class '$DSC' (provisioner $DPROV) created PV $PVN with hostPath='$HP' local='$LP'"
else
  record "OBSERVED  the probe pod on the default storage class did not become ready"
fi
kubectl -n default delete pod probe-default --wait --timeout=60s || true
kubectl -n default delete pvc probe-default --wait --timeout=60s || true

echo "== storage Velero can back up: static PersistentVolumes of type local (not hostPath)"
KNODE=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
docker inspect "$KNODE" >/dev/null 2>&1 || stop "no docker container is named like the node ($KNODE); the volume setup needs it"
if [ "$STORAGE" = "static" ]; then
for sc in $(kubectl get storageclass --no-headers | awk '/\(default\)/ {print $1}'); do
  kubectl patch storageclass "$sc" -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}' || true
done
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: velero-local
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
EOF
make_pv() { # name path: wipe the directory on the node and (re)create the PV
  docker exec "$KNODE" sh -c "rm -rf $2 && mkdir -p $2 && chmod 777 $2"
  kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolume
metadata:
  name: $1
spec:
  capacity:
    storage: 5Gi
  accessModes: ["ReadWriteOnce"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: velero-local
  local:
    path: $2
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: ["$KNODE"]
EOF
}
make_pv pv-db /mnt/pv-db
make_pv pv-neo /mnt/pv-neo
kubectl get pv,storageclass
else
  echo "== default storage mode: keeping the cluster's own storage class (no static volumes)"
  kubectl get storageclass
fi

echo "== S3 stand-in (RustFS)"
kubectl create namespace velero
kubectl -n velero apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: rustfs
spec:
  replicas: 1
  selector:
    matchLabels:
      app: rustfs
  template:
    metadata:
      labels:
        app: rustfs
    spec:
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: rustfs
          image: $S3_IMAGE
          args: ["/data"]
          env:
            - name: RUSTFS_ACCESS_KEY
              value: "$ACCESS_KEY"
            - name: RUSTFS_SECRET_KEY
              value: "$SECRET_KEY"
            - name: RUSTFS_ADDRESS
              value: ":9000"
            - name: RUSTFS_CONSOLE_ENABLE
              value: "false"
            - name: RUSTFS_OBS_LOGGER_LEVEL
              value: "error"
            - name: RUSTFS_OBS_LOG_DIRECTORY
              value: "/tmp/rustfs-logs/"
          ports:
            - containerPort: 9000
          readinessProbe:
            httpGet:
              path: /health
              port: 9000
            initialDelaySeconds: 5
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /data
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: data
          emptyDir: {}
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: rustfs
spec:
  selector:
    app: rustfs
  ports:
    - port: 9000
      targetPort: 9000
EOF
kubectl -n velero rollout status deploy/rustfs --timeout=240s
echo "S3 stand-in image: $(kubectl -n velero get pod -l app=rustfs -o jsonpath='{.items[0].status.containerStatuses[0].imageID}')"

MADE=""
for i in 1 2 3 4 5 6; do
  if kubectl -n velero run awscli-mb --rm -i --restart=Never --image=amazon/aws-cli:latest \
       --env="AWS_ACCESS_KEY_ID=$ACCESS_KEY" --env="AWS_SECRET_ACCESS_KEY=$SECRET_KEY" --env="AWS_DEFAULT_REGION=us-east-1" \
       -- --endpoint-url http://rustfs:9000 s3 mb s3://velero; then MADE=yes; break; fi
  sleep 10
done
[ "$MADE" = "yes" ] && record "PASS  S3 stand-in is up and the bucket was created" || { fail "could not create the bucket on the S3 stand-in"; exit 1; }

echo "== installing Velero (file-system backup through the node agent)"
printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' "$ACCESS_KEY" "$SECRET_KEY" > /tmp/velero-creds
velero install --provider aws \
  --image "velero/velero:$VELERO_VERSION" \
  --plugins "velero/velero-plugin-for-aws:$AWS_PLUGIN_VERSION" \
  --bucket velero --secret-file /tmp/velero-creds \
  --backup-location-config region=us-east-1,s3ForcePathStyle=true,s3Url=http://rustfs.velero.svc:9000 \
  --use-volume-snapshots=false --use-node-agent --default-volumes-to-fs-backup --wait
PHASE=""
for i in $(seq 1 24); do
  PHASE=$(kubectl -n velero get backupstoragelocation default -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [ "$PHASE" = "Available" ] && break
  sleep 5
done
if [ "$PHASE" = "Available" ]; then record "PASS  Velero is installed and its backup location is Available"; else fail "backup location phase is '$PHASE'"; exit 1; fi

echo "== workload: ragleap-ops chart (db and neo4j; app and voice at zero, private image)"
kubectl create namespace "$NS"
helm install ops "$OPS" -n "$NS" --set app.replicaCount=0 --set voice.replicaCount=0 --wait --timeout 600s
kubectl -n "$NS" get pods,pvc
kubectl -n "$NS" exec deploy/ragleap-db -- psql -U ragleap -d ragleap_core -v ON_ERROR_STOP=1 \
  -c "CREATE TABLE velero_probe (id int, note text);" \
  -c "INSERT INTO velero_probe VALUES (1, 'postgres-probe-row');" \
  -c "CHECKPOINT;" && record "PASS  wrote a probe row to Postgres" || { fail "could not write to Postgres"; exit 1; }
kubectl -n "$NS" exec deploy/ragleap-neo4j -- cypher-shell -u neo4j -p ragleapgraph \
  "CREATE (:VeleroProbe {note: 'neo4j-probe-node'});" && record "PASS  wrote a probe node to Neo4j" || { fail "could not write to Neo4j"; exit 1; }

echo "== backup"
velero backup create probe-backup --include-namespaces "$NS" --default-volumes-to-fs-backup --wait
BPHASE=$(kubectl -n velero get backup probe-backup -o jsonpath='{.status.phase}' 2>/dev/null || true)
if [ "$BPHASE" = "Completed" ]; then record "PASS  backup phase is Completed"; else fail "backup phase is '$BPHASE'"; fi
PVB=$(kubectl -n velero get podvolumebackups -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null || true)
NPVB=$(grep -c "Completed" <<< "$PVB" || true)
echo "pod volume backups: $(tr '\n' ' ' <<< "$PVB")"
if [ "$NPVB" -ge 2 ]; then record "PASS  $NPVB pod volume backups completed (volume data was backed up, not only objects)"
else fail "only $NPVB pod volume backups completed; the PVC data may have been skipped (Velero's file-system backup does not support hostPath volumes, which kind's default storage uses)"; fi

VOLS=$(kubectl -n velero get podvolumebackups -o jsonpath='{range .items[*]}{.spec.volume}{"="}{.status.phase}{"\n"}{end}' 2>/dev/null || true)
echo "pod volume backups by volume: $(tr '\n' ' ' <<< "$VOLS")"
if grep -q -x "db-data=Completed" <<< "$VOLS"; then record "PASS  the Postgres data volume (db-data) has a completed pod volume backup"; else fail "the db-data volume has no completed pod volume backup"; fi

echo "== disaster: delete the namespace"
kubectl get pv -o name | xargs -r -I{} kubectl patch {} -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}' && kubectl delete namespace "$NS" --wait --timeout=300s
if kubectl get namespace "$NS" >/dev/null 2>&1; then fail "namespace $NS still exists"; else record "PASS  namespace $NS is gone"; fi
if [ "$STORAGE" = "static" ]; then
echo "== the disks are lost too: replace both volumes with fresh empty ones"
kubectl delete pv pv-db pv-neo --wait --timeout=120s || true
make_pv pv-db /mnt/pv-db
make_pv pv-neo /mnt/pv-neo
if [ -z "$(docker exec "$KNODE" ls -A /mnt/pv-db)" ]; then record "PASS  the replacement database volume is empty (the restore must bring the data back)"; else fail "the replacement volume is not empty"; fi
else
  echo "== default storage: deleting the namespace deleted its claims; the volumes and their data directories must be gone"
  GONE=""; LEFT="?"; DIRS="?"
  for i in $(seq 1 24); do
    LEFT=$(kubectl get pv -o jsonpath='{range .items[*]}{.spec.claimRef.namespace}{"\n"}{end}' 2>/dev/null | grep -c -x "$NS" || true)
    DIRS=$(docker exec "$KNODE" sh -c "ls /var/lib/rancher/k3s/storage 2>/dev/null | grep -c _${NS}_" || true)
    if [ "$LEFT" = "0" ] && [ "$DIRS" = "0" ]; then GONE=yes; break; fi
    sleep 5
  done
  if [ "$GONE" = "yes" ]; then record "PASS  the volumes and their data directories are gone (the restore must bring the data back)"; else fail "volumes or data directories of $NS still exist (persistent volumes left: $LEFT, directories left: $DIRS)"; fi
fi

echo "== restore"
velero restore create probe-restore --from-backup probe-backup --wait
RPHASE=$(kubectl -n velero get restore probe-restore -o jsonpath='{.status.phase}' 2>/dev/null || true)
if [ "$RPHASE" = "Completed" ]; then record "PASS  restore phase is Completed"; else fail "restore phase is '$RPHASE'"; fi
if kubectl -n "$NS" wait --for=condition=Available deploy/ragleap-db --timeout=420s >/dev/null 2>&1; then record "PASS  ragleap-db is available after the restore"; else fail "ragleap-db is not available after the restore"; fi
if kubectl -n "$NS" wait --for=condition=Available deploy/ragleap-neo4j --timeout=420s >/dev/null 2>&1; then record "PASS  ragleap-neo4j is available after the restore"; else fail "ragleap-neo4j is not available after the restore"; fi

echo "== verify the data"
ROW=""
for i in 1 2 3 4 5 6; do
  ROW=$(kubectl -n "$NS" exec deploy/ragleap-db -c db -- psql -U ragleap -d ragleap_core -At -c "SELECT note FROM velero_probe WHERE id = 1;" 2>/tmp/pg.err || true)
  [ "$ROW" = "postgres-probe-row" ] && break
  sleep 10
done
echo "postgres says: [$ROW]"
if [ "$ROW" != "postgres-probe-row" ]; then echo "postgres stderr: $(cat /tmp/pg.err)"; fi
if [ "$ROW" = "postgres-probe-row" ]; then record "PASS  the Postgres probe row came back after the restore"; else fail "the Postgres probe row did not come back (got: $ROW)"; fi
NODE=""
for i in 1 2 3 4 5 6; do
  NODE=$(kubectl -n "$NS" exec deploy/ragleap-neo4j -c neo4j -- cypher-shell -u neo4j -p ragleapgraph --format plain "MATCH (n:VeleroProbe) RETURN n.note;" 2>/tmp/n4.err || true)
  grep -q "neo4j-probe-node" <<< "$NODE" && break
  sleep 10
done
echo "neo4j says: [$NODE]"
if ! grep -q "neo4j-probe-node" <<< "$NODE"; then echo "neo4j stderr: $(cat /tmp/n4.err)"; fi
if grep -q "neo4j-probe-node" <<< "$NODE"; then record "PASS  the Neo4j probe node came back after the restore"; else fail "the Neo4j probe node did not come back (got: $NODE)"; fi

echo
echo "== SUMMARY ($(date +%T))"
for r in "${RESULTS[@]}"; do echo "$r"; done
echo "Cluster: $CLUSTER. Storage mode: $STORAGE (static = hand-made local PersistentVolumes, default = the cluster's own dynamic storage; the OBSERVED line above says what the default storage creates). Velero file-system backup skips hostPath volumes."
echo "Not exercised: application-consistent backups (these are crash-consistent file copies of running databases; the runbook's pg_dump CronJob"
echo "remains the stronger guarantee), CSI snapshots, scheduled backups, restore into a different cluster, TTL and retention, real object storage, multi-node."
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
