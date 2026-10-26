# ragleap-ops

Kubernetes deployment manifests for RagLeap Core.

Ships Deployment + Service manifests for all four `docker-compose.yml`
services (`db`, `app`, `voice`, `neo4j`), translated from the real
compose file and live-tested end-to-end on a `kind` cluster — all four
pods reached `1/1 Running` with zero restarts after a probe-timing bug
was found and fixed via live testing.

<img src="https://mermaid.ink/svg/Zmxvd2NoYXJ0IFRECiAgICBjbGFzc0RlZiBzb3VyY2UgZmlsbDojMWUzYTVmLHN0cm9rZTojNGE5MGQ5LGNvbG9yOiNmZmYKICAgIGNsYXNzRGVmIG1hbmlmZXN0IGZpbGw6IzRkMzMxOSxzdHJva2U6I2Y1OWUwYixjb2xvcjojZmZmCiAgICBjbGFzc0RlZiBjaGFydCBmaWxsOiMzZDI2NDUsc3Ryb2tlOiNhODU1ZjcsY29sb3I6I2ZmZgogICAgY2xhc3NEZWYgY2x1c3RlciBmaWxsOiMxYTRkM2Esc3Ryb2tlOiMyMmM1NWUsY29sb3I6I2ZmZgoKICAgIENvbXBvc2VbImRvY2tlci1jb21wb3NlLnltbDxici8+cmVhbCBkYi9hcHAvdm9pY2UvbmVvNGogc2VydmljZXMiXTo6OnNvdXJjZSAtLT4gVHJhbnNsYXRlWyJGaWVsZC1ieS1maWVsZCB0cmFuc2xhdGlvbjxici8+bm8gaW52ZW50ZWQgdmFsdWVzIl06Ojpzb3VyY2UKCiAgICBUcmFuc2xhdGUgLS0+IE1hbmlmZXN0c1siazhzLyByYXcgbWFuaWZlc3RzPGJyLz5EZXBsb3ltZW50ICsgU2VydmljZSArIFBWQyArIFNlY3JldCJdOjo6bWFuaWZlc3QKICAgIFRyYW5zbGF0ZSAtLT4gQ2hhcnRbImhlbG0vcmFnbGVhcC1vcHMvIGNoYXJ0PGJyLz5jb25maWd1cmFibGUgdmFsdWVzLnlhbWwiXTo6OmNoYXJ0CgogICAgTWFuaWZlc3RzIC0tPiBPcmRlclsiQXBwbHkgb3JkZXI8YnIvPlNlY3JldHMvQ29uZmlnTWFwIOKGkiBQVkNzIOKGkiBkYiDihpIgYXBwL3ZvaWNlL25lbzRqIl06OjptYW5pZmVzdAogICAgQ2hhcnQgLS0+IEhlbG1JbnN0YWxsWyJoZWxtIGluc3RhbGwgLyBoZWxtIHVwZ3JhZGUiXTo6OmNoYXJ0CgogICAgT3JkZXIgLS0+IEtpbmRUZXN0WyJMaXZlIGtpbmQgY2x1c3RlciB0ZXN0Il06OjpjbHVzdGVyCiAgICBIZWxtSW5zdGFsbCAtLT4gS2luZFRlc3QKCiAgICBLaW5kVGVzdCAtLT4gVmVyaWZ5WyJBbGwgNCBwb2RzIDEvMSBSdW5uaW5nPGJyLz4wIHJlc3RhcnRzLCByZWFsIGJ1Z3MgZm91bmQgJiBmaXhlZCJdOjo6Y2x1c3Rlcgo=" alt="ragleap-ops translation and verification flow" width="100%">

## Contents

- `k8s/` — raw Deployment, Service, PVC, Secret, and ConfigMap manifests
- Apply order: Secrets/ConfigMap → PVCs → `db` → everything else
  (`app`/`voice` depend on `db` via an init container that polls
  `pg_isready`)

## Helm chart and hardening

`helm/ragleap-ops/` is the chart most users should install. It carries the
same services as `k8s/`, plus:

- **Zero-permission ServiceAccounts** for `db`, `app`, `voice` and `neo4j`
  (`serviceAccount.create`, default `true`). No Role or RoleBinding is
  created and no API token is mounted. Verified live: the token path does
  not exist in the `db` pod.
- **Upgrading from 0.3.0:** if the ServiceAccounts already exist from the raw
  `k8s/` manifests, Helm refuses with an ownership conflict. Install with
  `--set serviceAccount.create=false` (token mounting is still disabled), or
  delete the existing ServiceAccounts first.
- **Upgrading between published chart versions:** in CI on kind (Kubernetes
  v1.34.11, v1.36.4 and v1.37.0, a single-node cluster each), a
  `helm upgrade` from the published `0.5.2` to the `0.6.2` chart left the
  database pods, volumes and data untouched. That is one chart version pair;
  other pairs and upgrades that change values were not tested. A plain
  `helm upgrade` with no `--set` kept the replica counts set at install: a
  release installed with `--set app.replicaCount=0 --set voice.replicaCount=0`
  still had `app` and `voice` at zero after the upgrade, so pass the counts
  again (`--set app.replicaCount=1 --set voice.replicaCount=1`) when you want
  them running. Checked by the `chart-upgrade-test` workflow (runs 40221366116 and 40267480140).
- **Read-only root filesystem** for `db`, `app` and `voice`. It is
  deliberately not enabled for `neo4j`: live testing showed it shadows the
  image's default config (see the comment in `neo4j-deployment.yaml`).
- `RUNBOOK.md`: incident playbooks (db-down, neo4j-crash-loop, backup
  failure, expired ingress certificate), each drilled against a
  deliberately broken cluster.

## Regenerating local-only files

Two files are intentionally **not** committed (see `.gitignore`) since
they'd otherwise bake real secret values into git history:

```bash
kubectl create secret generic ragleap-app-env \
  --from-env-file=$HOME/ragleap-core/.env \
  --dry-run=client -o yaml > k8s/app-env-secret.yaml
```

## Secrets management for production (Sealed Secrets)

The kubectl create secret --from-env-file pattern above is fine for local
testing, but it leaves real credentials sitting as plaintext files and shell
history. For production, use Sealed Secrets instead (https://github.com/bitnami-labs/sealed-secrets) --
secrets get encrypted against your specific cluster's public key, so the
encrypted result is safe to commit to git. No other cluster can decrypt it.

One-time setup (per cluster):

```bash
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.39.1/controller.yaml
```

Sealing a secret (works offline once you have exported your cluster's public
cert -- no live cluster connection needed at seal-time, safe to run in CI):

```bash
kubeseal --controller-name sealed-secrets-controller \
  --controller-namespace kube-system --fetch-cert > sealed-secrets-pub-cert.pem

kubectl create secret generic ragleap-db-secret \
  --dry-run=client \
  --from-literal=POSTGRES_USER=ragleap \
  --from-literal=POSTGRES_PASSWORD=your-real-password \
  --from-literal=POSTGRES_DB=ragleap_core \
  -o yaml | kubeseal --format yaml --cert sealed-secrets-pub-cert.pem > db-secret-sealed.yaml

kubectl apply -f db-secret-sealed.yaml
```

Important: a sealed secret is tied to the exact cluster whose public key
encrypted it. A SealedSecret sealed for one cluster will not decrypt on a
different cluster -- never copy a sealed file between environments; reseal
against each target cluster's own cert instead.

## Status

Both the raw manifests (v0.1.0) and the Helm chart (v0.2.0, `helm/ragleap-ops/`) are live-verified end-to-end on a real kind cluster — see CHANGELOG.md for the specific bugs found and fixed.

## NetworkPolicies (restricting pod-to-pod traffic)

Both k8s/ and helm/ragleap-ops/ include NetworkPolicy resources restricting
which pods can reach ragleap-db (port 5432, from ragleap-app and
ragleap-voice only) and ragleap-neo4j (ports 7474/7687, from ragleap-app
only -- ragleap-voice does not use neo4j in the current codebase).

Important: kind's default CNI (kindnet) does not enforce NetworkPolicy at
all -- policies will apply without error but traffic will not actually be
blocked. Testing NetworkPolicy enforcement requires a CNI that supports it,
such as Calico:

```bash
kind create cluster --config kind-calico-config.yaml   # disableDefaultCNI: true
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/tigera-operator.yaml
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/custom-resources.yaml
```

Verification performed: the enforcement mechanism itself was live-tested end
to end on a real kind + Calico cluster (a labeled pod was correctly blocked
from an unlabeled target, then allowed once correctly labeled -- confirmed
via real timeout/success exit codes, not assumed). The real ragleap-db/
ragleap-neo4j label selectors were verified to match the actual Deployment
labels used elsewhere in this chart. A full live run of the complete
ragleap stack under Calico was not completed in this session due to
genuine memory constraints on the test VPS (which also runs unrelated
production services) -- Calico's own baseline overhead left too little
headroom for a 4-service stack reliably. This is an honest scope
boundary, not a claim that the policies were fully integration-tested
against the live application.

## Ingress + TLS

The Helm chart includes an optional Ingress + cert-manager Certificate
(disabled by default via `ingress.enabled: false`). Enabling it requires:

1. An NGINX Ingress Controller installed on your cluster
2. cert-manager installed, with a real ClusterIssuer configured (Let's
   Encrypt for production, or a self-signed issuer for local testing)
3. A real hostname you own, set via `ingress.hostname` in values.yaml
4. Your ClusterIssuer's name set via `ingress.clusterIssuer`

Example production ClusterIssuer (Let's Encrypt, replace the email):

```bash
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
EOF
```

Then deploy with:

```bash
helm install ragleap-ops ./ragleap-ops \
  --set ingress.enabled=true \
  --set ingress.hostname=your-real-domain.com \
  --set ingress.clusterIssuer=letsencrypt-prod
```

Verification performed: the full Ingress + TLS chain was live-tested end to
end on a real kind cluster with a genuine NGINX Ingress Controller and
cert-manager, using a self-signed ClusterIssuer (Let's Encrypt itself
requires public DNS and internet-reachable ports, which a local kind
cluster cannot satisfy for a real ACME challenge). Confirmed via openssl:
the correct certificate was served over TLS via SNI, matching the exact
hostname requested, not a generic fallback certificate. HTTP routing
through the Ingress to the backend service was independently confirmed
working. Production Let's Encrypt issuance was not live-tested in this
session for the reason above -- the issuer configuration shown is the
standard, documented cert-manager pattern, not independently verified
against a real public domain here.

## Multi-environment deployment

Environment-specific overrides live in `helm/ragleap-ops/values-{dev,staging,prod}.yaml`,
layered on top of the base `values.yaml` (never edit the base file for a
single environment):

```bash
helm install ragleap-ops ./ragleap-ops -f values-dev.yaml       # local/dev
helm install ragleap-ops ./ragleap-ops -f values-staging.yaml   # staging
helm install ragleap-ops ./ragleap-ops -f values-prod.yaml      # production
```

`db` and `neo4j` intentionally have no `replicaCount` in any environment
file and stay hardcoded at 1 in the templates -- both are stateful,
single-writer services backed by `ReadWriteOnce` PVCs. Horizontal scaling
for either would require a genuinely different storage/clustering
architecture, not a values.yaml change, so this isn't offered as a
configurable option that would silently produce a broken multi-writer
setup if someone bumped the number.

Verified: rendered output for `app.replicaCount`/`voice.replicaCount`
and `ingress.enabled`/`ingress.hostname` confirmed to differ correctly
across all three overlay files via `helm template -f values-<env>.yaml`
-- dev (1/1, no ingress), staging (1/1, ingress enabled with its own
hostname), prod (3/2, ingress enabled with its own hostname).

## Backup / Disaster Recovery

Scheduled backups for both `db` (Postgres) and `neo4j` live in
`k8s/backup-pvc.yaml`, `k8s/db-backup-cronjob.yaml`, and
`k8s/neo4j-backup-cronjob.yaml`.

**Postgres**: `pg_dump` runs against the live database daily -- no
downtime, Postgres handles concurrent dumps natively via MVCC.

**Neo4j**: Community Edition has no online/hot backup command (`neo4j-admin
database backup` is Enterprise-only, confirmed via `--help` against the
real image -- only `dump` exists in Community). The CronJob scales
`ragleap-neo4j` to 0 replicas, waits for the pod to fully terminate, then
runs `neo4j-admin database dump` against the now-unlocked PVC via a Job
with the same volume mounted.

**Important design decision -- fail loud, no automatic restart:** if the
dump step fails, neo4j is intentionally left scaled to 0 rather than
automatically restored. Coupling application uptime to backup success
was considered and rejected -- a silently-failing backup with automatic
scale-up could leave a real backup gap unnoticed for a long time while
the app appears healthy. Monitor CronJob/Job success explicitly
(`kubectl get cronjobs`, `kubectl get jobs`, or your alerting stack) and
manually restore service after confirming/investigating a failure:

```bash
kubectl scale deployment ragleap-neo4j --replicas=1
```

Verification performed: both `pg_dump` and the scale-to-zero +
`neo4j-admin dump` pattern were live-tested against real running
instances on a real kind cluster -- `pg_dump` produced a valid dump
against the live database; the neo4j pattern produced a real, complete
257.8MiB dump (36 files) via the Job mechanism. The scheduled CronJob
wrapper itself (RBAC, scale-down init container, timing) was validated
structurally (`python3 -c "import yaml..."` parse + document/kind
checks) but not exercised end-to-end via an actual cron trigger this
session.

## Autoscaling and disruption budgets

The stateless `app` and `voice` workloads can optionally use a HorizontalPodAutoscaler and a PodDisruptionBudget. Both are off by default, and `db` and `neo4j` are never autoscaled.

```yaml
app:
  resources:
    requests: { cpu: 200m, memory: 128Mi }   # required for CPU-percentage autoscaling
  autoscaling:
    enabled: true
    minReplicas: 2            # default 1
    maxReplicas: 6            # required
    targetCPUUtilizationPercentage: 70   # default 80
  pdb:
    enabled: true
    minAvailable: 1           # default 1
```

Notes:

- When autoscaling is enabled, `replicaCount` is ignored for that workload.
- Autoscaling needs metrics-server in the cluster.
- A PDB with `minAvailable: 1` on a single-replica workload blocks voluntary node drains.
- Scale-up and scale-down were verified live for `app` on `kind`. `voice` was verified by render only, and PDB behaviour during a real drain was not tested.
