# ragleap-ops — Incident Runbook

Operational playbooks for the 4 minimum incident scenarios flagged in
the DevOps maturity plan: db-down, neo4j-crash-loop,
backup-failure-detected, ingress-cert-expired.

**Status: written from real config in this repo, not yet independently
live-tested against a deliberately broken cluster.** Every command
below references real resource names verified against the actual
manifests (see file paths cited in each section) -- but "the runbook
reads correctly" and "the runbook works when someone is stressed at
3am" are different claims. Treat this as a strong first draft, not a
drilled-and-proven procedure. A live incident drill (deliberately
breaking each scenario on a test cluster and following these exact
steps) is the natural next step before trusting this fully -- same
discipline as everything else in this repo.

---

## 1. db-down

**Symptom:** `ragleap-app`/`ragleap-voice` failing to connect, or
`kubectl get pods -n ragleap-core` shows `ragleap-db` not `Running`.

### Triage

```bash
kubectl get pods -n ragleap-core -l app=ragleap-db
kubectl describe pod -n ragleap-core -l app=ragleap-db
kubectl logs -n ragleap-core -l app=ragleap-db --tail=100
```

Check `Events` in the describe output first -- most failures show up
there before you need full logs.

**These three commands are NOT sufficient on their own -- live-tested
and confirmed to miss a real incident.** A deliberately broken
`POSTGRES_PASSWORD` (simulating credential drift/rotation gone wrong)
produced a pod that stayed `1/1 Running`, `Ready: True`, `Events:
<none>` in describe output, and logs fully drowned in the same
harmless `pg_isready` noise already documented below -- the real
incident was completely invisible to these three commands. This is
because `pg_isready` only checks the server accepts TCP connections,
not that real credentials actually work, and `pg_hba.conf`'s default
`trust` rule for `127.0.0.1` connections meant testing from inside the
pod itself gave a false all-clear too.

**Always also run a real, external connectivity test using the actual
configured credentials -- not just pod status.** Read the real
credential locally first, then pass it into the debug pod directly --
an earlier version of this runbook tried fetching the secret from
*inside* the debug pod via a nested `kubectl` call, which failed
outright (`kubectl: not found` -- the postgres:16-alpine image has no
kubectl binary). Fixed and re-verified live:

```bash
DB_PASSWORD=$(kubectl get secret ragleap-db-secret -n ragleap-core -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
kubectl run debug-psql --rm -it --image=postgres:16-alpine --restart=Never -n ragleap-core \
  --env="PGPASSWORD=$DB_PASSWORD" -- psql -h ragleap-db -U ragleap -d ragleap_core -c "SELECT 1;"
```

A real, useful error (`FATAL: password authentication failed`) means
the credentials are genuinely wrong -- something `kubectl get pods`
alone will never show you.

### Common causes (from real testing this session)

- **`CrashLoopBackOff` with `Liveness probe failed: command "pg_isready -U ragleap" timed out`**
  -- seen this session after a laptop/Docker Desktop sleep interruption
  caused sustained resource starvation. Not a data problem. Fix:
```bash
  kubectl delete pod -n ragleap-core -l app=ragleap-db
```
  Kubernetes will recreate it via the Deployment. Confirm the underlying
  host (VM, laptop, node) isn't itself under resource pressure before
  assuming this is a code/config issue.

- **`chmod: Operation not permitted` in early startup logs** -- this is
  expected, non-fatal noise from `readOnlyRootFilesystem`, not the
  actual failure. If the pod reaches `Running` afterward, this line is
  not the incident.

- **`FATAL: database "ragleap" does not exist` repeating in logs** --
  also expected noise: the liveness probe's `pg_isready -U ragleap`
  checks for a database matching the *username*, not the real
  configured database (`ragleap_core`). Harmless as long as
  `pg_isready` itself reports `accepting connections`.

- **Init container stuck (`Init:CreateContainerConfigError`)** -- check
  `securityContext`. Confirmed live-tested bug pattern this session:
  `runAsNonRoot: true` without a matching `runAsUser` fails outright
  against root-default images. Confirm the real UID before assuming a
  fix: `docker run --rm <image> id <user>` -- never guess.

### If genuinely down (not just restarting)

1. Check the PVC is bound: `kubectl get pvc -n ragleap-core ragleap-db-data`
2. If the PVC itself is the problem (rare, but possible on `kind` or
   dev clusters), see the restore procedure in the Backup/DR section
   of the root README -- this is the last resort, not step one.

---

## 2. neo4j-crash-loop

**Live-tested this session -- these triage steps worked correctly.**
Deliberately set a malformed `NEO4J_AUTH` value (missing the required
`/` separator) to produce a genuine `CrashLoopBackOff`, then followed
the documented triage steps below exactly as written. Unlike the
db-down and backup-failure sections (both of which had real command
errors found and fixed this session), this section's `kubectl logs`
command surfaced the real, actionable error immediately and clearly:
`Invalid value for NEO4J_AUTH: 'this-is-not-valid-auth-format'`.
Confirmed recovery afterward: real Cypher query succeeded once the
correct secret was restored.

**Symptom:** `ragleap-neo4j` pod in `CrashLoopBackOff` or repeatedly
restarting.

### Triage

```bash
kubectl get pods -n ragleap-core -l app=ragleap-neo4j
kubectl logs -n ragleap-core -l app=ragleap-neo4j --tail=100
kubectl describe pod -n ragleap-core -l app=ragleap-neo4j
```

### Common causes

- **Slow startup being mistaken for a crash.** Neo4j is genuinely
  slower to start than Postgres -- this session it took roughly 90
  seconds to reach `1/1 Running` on a real kind cluster. Check the
  `readinessProbe`/`livenessProbe` `initialDelaySeconds` in
  `k8s/neo4j-deployment.yaml` before assuming a real failure; a
  too-aggressive probe timeout on a slow node will kill a
  genuinely-still-starting pod.

- **Permission errors on `/data`.** The `fix-permissions` init
  container (`chown -R 7474:7474 /data`) must complete successfully
  first. Check its logs specifically:
```bash
  kubectl logs -n ragleap-core -l app=ragleap-neo4j -c fix-permissions
```

- **If `readOnlyRootFilesystem` is ever enabled on this service in the
  future:** this repo's own testing found Neo4j's entrypoint rewrites
  `/var/lib/neo4j/conf` on every startup. If that flag gets enabled
  without first confirming `/var/lib/neo4j/conf` is writable, this is
  the first thing to suspect -- see CHANGELOG.md's "Known limitations"
  for the exact unresolved question.

### If genuinely down (not just restarting)

Same PVC-check-first principle as db-down. See the root README's
Backup/DR section for the neo4j restore procedure
(`neo4j-admin database load`) if data-level recovery is needed --
confirmed via real live testing to be cleaner than the Postgres
restore path (zero errors on load, vs. expected-but-harmless
`already exists` noise for Postgres).

---

## 3. backup-failure-detected

**Symptom:** A scheduled backup CronJob run failed, or hasn't produced
a new file when expected.

### Triage

```bash
kubectl get cronjob -n ragleap-core ragleap-db-backup ragleap-neo4j-backup
kubectl get jobs -n ragleap-core -l job-name
kubectl logs -n ragleap-core -l job-name=<failed-job-name>
```

Both backup CronJobs run daily at `0 3 * * *` UTC
(`k8s/db-backup-cronjob.yaml`, `k8s/neo4j-backup-cronjob.yaml`).

### Postgres backup (`ragleap-db-backup`)

- Straightforward `pg_dump` to a PVC-backed file
  (`/backups/db-<timestamp>.sql`). Failure usually means either the
  `ragleap-db-secret` credentials are wrong/rotated, or the
  `ragleap-backup-data` PVC is full or unbound.
- **Check disk space -- live-tested and corrected.** The original
  version of this step tried `kubectl exec ... deploy/ragleap-db -- df
  -h /backups`, which is simply wrong: `/backups` is never mounted
  into `ragleap-db` at all, only into the backup CronJob's own pod.
  Confirmed live (`df: /backups: No such file or directory`). The
  documented fallback (an inline `--overrides` JSON one-liner) is
  also fragile across shells -- confirmed failing under PowerShell
  quoting. Replaced with a small, portable YAML file instead, matching
  this project's own established pattern of writing anything
  non-trivial to a real file rather than fighting inline quoting:
```bash
cat > /tmp/debug-pvc.yaml << 'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: debug-pvc
  namespace: ragleap-core
spec:
  restartPolicy: Never
  containers:
    - name: debug-pvc
      image: busybox
      command: ["df", "-h", "/backups"]
      volumeMounts:
        - name: b
          mountPath: /backups
  volumes:
    - name: b
      persistentVolumeClaim:
        claimName: ragleap-backup-data
EOF
kubectl apply -f /tmp/debug-pvc.yaml
kubectl logs -n ragleap-core debug-pvc
kubectl delete pod -n ragleap-core debug-pvc
```

Live-verified this session: real output showed `944.4G` available on
a `1006.9G` volume -- confirming the pattern genuinely works.

### Neo4j backup (`ragleap-neo4j-backup`)

More involved -- this job **scales `ragleap-neo4j` to 0 replicas
first** (via a dedicated `ragleap-neo4j-backup` ServiceAccount/Role),
takes the dump, and does not automatically scale it back up (the job
only scales down; nothing in this CronJob restores replicas). **If a
neo4j backup job fails partway through, this is now handled
automatically -- see below -- but it's still worth knowing how to
check manually:**

```bash
kubectl get deployment -n ragleap-core ragleap-neo4j
# if REPLICAS shows 0/0 unexpectedly:
kubectl scale deployment -n ragleap-core ragleap-neo4j --replicas=1
```

**Fixed, live-tested on a real kind cluster (both success and failure
paths):** the CronJob now includes a `scale-up-watcher` native sidecar
container (`restartPolicy: Always`) that is only terminated after the
`dump` container finishes, success or failure. `dump` uses
`trap 'touch /signal/backup-done' EXIT` to always signal completion;
the sidecar waits for that signal, then scales `ragleap-neo4j` back to
1. Verified live: a deliberately-broken dump (nonexistent database
name) correctly reported the Job as `Failed`, while `ragleap-neo4j`
was still confirmed `1/1 Running` afterward -- the manual recovery
command above should now only be needed if the sidecar itself is
somehow prevented from running (e.g. the whole pod being forcibly
deleted), not for an ordinary dump failure.

### General backup verification

Don't just trust "the CronJob succeeded" -- per this repo's own
discipline, verify the actual file:

```bash
kubectl exec -n ragleap-core -it deploy/ragleap-db -- ls -la /backups/  # if db pod still has the mount
```

or run a debug pod against the `ragleap-backup-data` PVC directly if
the primary pods aren't available.

---

## 4. ingress-cert-expired

**Live-tested this session (using cert-manager installed directly on
the kind cluster with a self-signed test ClusterIssuer, since testing
the real Certificate lifecycle does not require a working ingress
controller or real DNS).** Deliberately pointed a real Certificate at
a nonexistent ClusterIssuer to simulate a real, common failure (issuer
typo'd or accidentally deleted). All commands below worked and,
together, correctly diagnosed the real cause -- no command errors
found this time, unlike the db-down and backup-failure sections. One
real clarity gap found and fixed: the original version did not
explain where to find the issuer name for the final command -- it
comes from the certificaterequest step's ISSUER column, not from
guessing.

**Symptom:** TLS errors on the real hostname, or
`kubectl get certificate -n ragleap-core ragleap-app-tls` shows
`Ready: False`.

### Triage

```bash
kubectl get certificate -n ragleap-core ragleap-app-tls
kubectl describe certificate -n ragleap-core ragleap-app-tls
kubectl get certificaterequest -n ragleap-core
```

The `describe certificate` step tells you *that* something is wrong
(e.g. `Reason: IncorrectIssuer`) but usually not enough detail to act
on. **The `certificaterequest` step is where the real diagnosis
lives** -- its `ISSUER` column shows the actual issuer name the
Certificate is currently trying to use, which is the name to pass into
the next command:

```bash
kubectl describe clusterissuer <issuer-name-from-the-certificaterequest-output-above>
```

A real `Error from server (NotFound): clusterissuers.cert-manager.io
"<name>" not found` confirms the issuer itself is missing or
misconfigured -- not the Certificate resource.

The Certificate resource name (`ragleap-app-tls`) and Secret name
(`ragleap-app-tls-secret`) are fixed in
`helm/ragleap-ops/templates/ingress.yaml` -- not configurable via
`values.yaml`, only the hostname and issuer name are.

### Common causes

- **`ClusterIssuer` itself is broken** (rate-limited by Let's Encrypt,
  misconfigured DNS-01/HTTP-01 solver, expired issuer credentials).
  Check `kubectl describe clusterissuer` for real error messages before
  assuming the Certificate resource itself is at fault.
- **DNS not actually pointing at the Ingress controller.** cert-manager
  cannot complete an HTTP-01 challenge if the hostname doesn't resolve
  to the real ingress IP. Verify with `dig`/`nslookup` against the real
  hostname in `values.yaml`'s `ingress.hostname`.
- **Automatic renewal didn't fire in time.** cert-manager renews
  ~30 days before expiry by default; if a Certificate is close to
  expiring with no recent `CertificateRequest`, something is blocking
  the renewal attempt itself, not just the cert.

### Manual force-renewal (last resort)

```bash
kubectl delete secret -n ragleap-core ragleap-app-tls-secret
kubectl delete certificate -n ragleap-core ragleap-app-tls
kubectl apply -f - <<EOF
# Re-apply via helm upgrade instead of hand-editing -- ensures the
# Certificate resource matches the real values.yaml, not a stale copy
EOF
helm upgrade ragleap-ops ./helm/ragleap-ops -f <your-values-file>
```

Deleting the Secret and Certificate forces cert-manager to request a
fresh certificate on the next reconcile. This is disruptive (brief TLS
outage while the new cert issues) -- only use if the automatic renewal
path is confirmed broken, not as a routine fix.

---

## Appendix A. Velero: optional cluster-level backup (not an incident scenario)

**Status: tested in CI on throwaway kind and k3d (k3s in Docker) clusters.
Not tested on a systemd k3s install, on a cloud cluster, or with real object
storage.**

The daily CronJobs in section 3 (`pg_dump` for Postgres, the
`neo4j-admin` dump for Neo4j) stay the primary, application-consistent
backups. Velero is an optional second layer: it backs up a whole
namespace (objects and volume data) so it can be restored onto a fresh
cluster or empty disks. Velero's file-system backup copies the files of
running databases, so those copies are crash-consistent, not
application-consistent. It does not replace the CronJobs.

**The trap (observed in CI): a `Completed` backup can contain no
database data.** In the CI test, Velero's file-system backup left out
the `hostPath` volumes. This chart's PVCs use the cluster's default StorageClass. On kind that
is `rancher.io/local-path`, and the CI test observed that it creates
`hostPath` PersistentVolumes. With that storage, the backup reported
`Completed`, contained only emptyDir volumes, and the restored
databases were empty. K3s uses a provisioner with the same name, but the CI
test on k3s v1.34.12 (in Docker, through k3d) observed that its default
storage class `local-path` created `local` PersistentVolumes, not `hostPath`
ones. So the trap was seen on kind's default storage only: check your own
cluster with the command below. On that k3s default storage the Velero
test passed too (see "What was verified").

**What was verified** (`.github/scripts/velero-test.sh`, workflow
`velero-test`; four passing runs on GitHub runners, 2026-10-19 and
2026-10-20): Velero v1.18.3 with the AWS plugin v1.14.0 and a RustFS
S3-compatible stand-in, on kind, using static PersistentVolumes of type
`local`. The test writes a row to Postgres and a node to Neo4j, backs up
the namespace with file-system backup, deletes the namespace, replaces
both volumes with fresh empty ones, restores, and checks that both
records came back.

The same test also passed once on k3s v1.34.12 in Docker (k3d v5.9.0, run
40087055391, 2026-10-24), again with static `local` PersistentVolumes.

It also passed on k3s's own default dynamic storage (`local-path`, which created
`local` PersistentVolumes; no static volumes), on the same k3d cluster, through the
workflow input `storage=default` (runs 42749230898 and 42750271654, 2026-11-10). In that
mode the test first checks that deleting the namespace removed the volumes and their
data directories, then restores, and both records came back. The checks were shown to
fail when the guarded step is broken: volumes kept after the namespace was deleted
(run 42750190622), a restore without pods (run 42750219327) and a backup without the
file-system flag (run 42750247722). This is one cluster, one Velero version and one
node, not a statement about every k3s.

**How to check that a backup really contains your data**

```bash
# every data volume must be listed and Completed (emptyDir volumes such as
# tmp also appear and do not count)
kubectl -n velero get podvolumebackups \
  -o jsonpath='{range .items[*]}{.spec.volume}={.status.phase}{"\n"}{end}'
# expect db-data=Completed and neo4j-data=Completed

# what kind of volume backs your PVCs? hostPath volumes were left out in CI on kind
kubectl get pv <pv-name> -o jsonpath='{.spec.hostPath.path}'   # non-empty means hostPath
```

Then prove it by restoring: restore into an empty namespace on empty
volumes and query the data, as the CI test does. A backup that has
never been restored is not a backup.

**Not covered:** application-consistent backups, CSI snapshots,
scheduled backups, retention, restoring into a different cluster,
real object storage, multi-node clusters, a systemd k3s install, and
more than one k3s version (the dynamic-storage run used k3s v1.34.12 only).

---

## Standing principle for all four scenarios

Per this repo's own operating discipline: don't assume a fix worked
because a command completed without error. Confirm the actual
end state -- pod `Running` with 0 new restarts, backup file genuinely
present and non-empty, certificate `Ready: True`, real connectivity
confirmed -- the same way every fix in this repo's CHANGELOGs has been
independently re-verified after being applied, not just trusted.
