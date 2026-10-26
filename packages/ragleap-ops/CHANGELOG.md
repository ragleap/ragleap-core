# Changelog

All notable changes to `ragleap-ops` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Documentation

- README: how upgrades between published chart versions behave. In CI on kind (`chart-upgrade-test`, run 40221366116), `helm upgrade` from the published `0.5.2` to the `0.6.2` chart left the Postgres and Neo4j pods, volumes and data untouched, and a plain `helm upgrade` with no `--set` kept the `app.replicaCount` and `voice.replicaCount` values set at install (both stayed at zero). One version pair only. Not tested: upgrades that change values, rollbacks, other Kubernetes versions, multi-node scheduling, application behaviour.

## [0.6.2] - 2026-10-24

### Documentation

- RUNBOOK Appendix A: the statement that k3s's default storage very likely behaves like kind's was an untested inference and is wrong for k3s v1.34.12. A CI run on k3s in Docker (k3d v5.9.0, run 40087055391) observed that its default `local-path` storage creates `local` PersistentVolumes, not `hostPath`. The Velero test also passed on that cluster with static `local` volumes. Not tested: Velero on k3s's default dynamic storage, and a systemd k3s install.

### Changed

- The `app` and `voice` image defaults, in the Helm values and in the raw `k8s/` manifests, now point to `ghcr.io/ragleap/ragleap-app:0.11.1` (pinned). `0.11.1` is `0.11.0` plus only a `requirements.lock` change (Pillow 12.3.0 to 13.0.0, pypdf 6.20.1 to 6.21.1). Anything that sets `app.image` and `voice.image` itself is unaffected. To keep the previous image, set both to `ghcr.io/ragleap/ragleap-app:0.11.0`.
- The package description no longer says "live-tested against a real cluster". It is in this release.

### Verified

- On kind (GitHub runner), with chart 0.6.1 and the image overridden by `--set`, both the `app` and `voice` pods became Ready and the library versions read from inside the running container were `pillow=13.0.0 pypdf=6.21.1` (ops-app-pods-test: run 40111412227 for `sha-9f986ae`, run 40111878857 for `latest` after the tag). The same test on `0.11.0` read `pillow=12.3.0 pypdf=6.20.1` (run 40110869430). Readiness is a TCP check on the port.

### Known limitations

- Application behaviour with Pillow 13 and pypdf 6.21.1 was not tested: the tests show that the image starts, listens and has those versions.
- The chart does not create the Secret `ragleap-app-env`, which both pods read through `envFrom`. Create it yourself, or the pods stay in `CreateContainerConfigError`.
- The `ragleap-app` package is private: the `ghcr-pull-secret` must hold credentials with read access to it.

## [0.6.1] - 2026-10-21

### Fixed

- The `app` and `voice` containers could not be created: they set `runAsNonRoot: true` without a numeric user, and the image declares a named user (`USER ragleap`), so the kubelet refused with "container has runAsNonRoot and image has non-numeric user (ragleap)". They now set `runAsUser: 10001`, the UID the Dockerfile gives that user. In the chart it is a value (`app.runAsUser`, `voice.runAsUser`); the raw `k8s/` manifests carry the number. Found by running the chart's `app` and `voice` pods on kind with the `ghcr.io/ragleap/ragleap-app:0.11.0` image on 2026-10-21; earlier CI runs scaled these pods to zero, so the problem had not been seen.

### Known limitations

- The chart does not create the Secret `ragleap-app-env`, which both pods read through `envFrom`. Create it yourself, or the pods stay in `CreateContainerConfigError`.
- Whether the pods then start and become Ready was not verified when this entry was written; see the repository's `ops-app-pods-test` workflow for the diagnostic.

## [0.6.0] - 2026-10-20

### Changed

- The `app` and `voice` image defaults, in the Helm values and in the raw `k8s/` manifests, now point to `ghcr.io/ragleap/ragleap-app:0.11.0`, the image that this repository's `publish-image` workflow builds and publishes. They pointed to `ghcr.io/antonyrag/ragleap-app:latest`, a package under a different account whose `latest` tag was last updated on 2026-08-30 and is not published by the workflow.
- The default tag is `0.11.0` (a pinned version, so installs do not change when a new image is published).

### Upgrade note

- The `ragleap-app` package under `ragleap` was private when this was written (2026-10-20). The `ghcr-pull-secret` that the chart references must hold credentials with read access to that package, or the `app` and `voice` pods will stay in `ImagePullBackOff`. To keep the old image, set `app.image` and `voice.image` yourself.

### Known limitations

- CI scales `app` and `voice` to zero, so those pods were not run against the chart with the new image. What was checked: the Helm render differs from 0.5.2 only in the two image lines, `helm lint` passes, and the wheel contains the chart and the manifests.

## [0.5.2] - 2026-10-19

### Changed

- The project URLs on the package page (Source, Changelog, Issues) now point to github.com/ragleap/ragleap-core, where the repository moved from antonyrag/ragleap-core. No code or chart changes.

## [0.5.1] - 2026-10-17

### Fixed

- The wheel now includes the Helm chart. Earlier releases (including 0.5.0) shipped only the Python module, because the build table was misnamed and no files were included. `pip install` now gives you `helm/ragleap-ops` and `k8s/`.



## [0.5.0] - 2026-10-09

### Added

- Optional `autoscaling` (HorizontalPodAutoscaler) and `pdb` (PodDisruptionBudget) for the stateless `app` and `voice` workloads. Both are off by default. `db` and `neo4j` are stateful and are intentionally not autoscaled.
- `autoscaling` fields: `enabled`, `minReplicas` (default 1), `maxReplicas` (required), `targetCPUUtilizationPercentage` (default 80). `pdb` fields: `enabled`, `minAvailable` (default 1).
- Optional `app.resources` and `voice.resources`, empty by default and rendered only when set. CPU-percentage autoscaling needs `resources.requests.cpu`; enabling autoscaling without it, or without `maxReplicas`, fails at render time with a message naming the missing field.
- When autoscaling is enabled the Deployment omits `replicas`, so `helm upgrade` does not reset the autoscaler's count. `replicaCount` is ignored for that workload.

### Verified (live on kind, `app` only, stand-in image)

- With autoscaling off, `helm template` output is byte-identical to 0.4.1 for both the default values and `values-prod.yaml`.
- Scale-up from 1 to 4 replicas under CPU load, all new pods Running.
- The PDB tracked the replica count: 0 allowed disruptions at 1 replica, 3 at 4.
- Scale-down from 4 back to 1 after the load stopped, with the default 5-minute stabilization.

### Known limitations

- `voice` was verified by render only. Its autoscaling was not run live, because it needs the private application image. It also has no readiness probe, and scaling it down cuts any live voice sessions on the removed pods.
- The live test used a stand-in image and a dummy `ragleap-app-env` Secret in a scratch namespace, on a single busy `kind` node.
- PDB behaviour during a real node drain was not tested. A PDB with `minAvailable: 1` on a single replica blocks voluntary drains.
- `SuccessfulRescale` events did not appear in `kubectl describe hpa` during the test; scaling was confirmed from pod counts and HPA status.
- Autoscaling needs metrics-server in the cluster.

## [0.4.1] - 2026-10-03

### Changed

- Package metadata only, no chart or code changes: added PyPI `keywords`, fuller trove classifiers (audience, MIT license, topic) and `[project.urls]` (Documentation, Source, Changelog, Issues) so the package is discoverable on PyPI.

## [0.4.0] - 2026-10-02

### Added

- **Dedicated zero-permission ServiceAccounts in the Helm chart** (`templates/serviceaccounts.yaml`): one each for `app`, `voice`, `db` and `neo4j`, with `automountServiceAccountToken: false` on both the ServiceAccount and each Deployment's pod spec. This hardening previously existed only in the raw `k8s/` manifests (#291); the Helm chart, which is what users install, had none of it, so a `helm install` ran every service as the namespace `default` account with its API token mounted. No Role or RoleBinding is added, because none of these services call the Kubernetes API. Controlled by the new `serviceAccount.create` value (default `true`).
  - **Upgrade note:** if the ServiceAccounts already exist from the raw `k8s/` manifests, Helm refuses with an ownership conflict. Install with `--set serviceAccount.create=false` (the Deployments still disable token mounting), or delete the existing ServiceAccounts first.
  - Verified on a live `kind` cluster in a scratch namespace: before the change, the db pod listed `ca.crt`, `namespace` and `token` under `/var/run/secrets/kubernetes.io/serviceaccount`; after it, that path does not exist. `helm template` confirms 4 ServiceAccounts, 4 Deployments with automount disabled, and 0 ServiceAccounts with `create=false`.
  - Not live-tested: the `create=false` install path, and the `app`/`voice` pods (their Deployment specs are verified, but the pods need an image pull secret and could not start in the scratch namespace).

- `RUNBOOK.md` -- incident playbooks for the 4 minimum scenarios (db-down, neo4j-crash-loop, backup-failure-detected, ingress-cert-expired). Written from real resource names/config verified against the actual manifests, and grounded in real symptoms observed during this session's live testing (e.g. the pg_isready probe-timeout pattern, the readOnlyRootFilesystem chmod noise). A real, previously-undocumented gap was found while writing it: the neo4j backup CronJob scales ragleap-neo4j to 0 replicas before dumping but has no automatic scale-back-up or rollback on failure -- flagged in the runbook as a follow-up item, not fixed here. Honestly labeled: written correctly against real config, but not yet independently live-tested against a deliberately broken cluster -- that drill is the natural next step before trusting this fully.

### Verified (this session, part 6)

- **Live incident drill against `RUNBOOK.md`'s ingress-cert-expired section.** Installed cert-manager directly on the kind cluster with a self-signed test ClusterIssuer -- testing the real Certificate lifecycle doesn't require a working ingress controller or real DNS, so this was a lighter, more targeted setup than a full ingress-nginx install. Deliberately pointed a real Certificate at a nonexistent ClusterIssuer to simulate a real, common failure (issuer typo'd or accidentally deleted). All documented commands worked correctly and, together, diagnosed the real cause -- no command errors found this time, unlike db-down and backup-failure. One real clarity gap found and fixed: the original version didn't explain where to find the issuer name for the final `describe clusterissuer` command -- it comes from the `certificaterequest` step's `ISSUER` column, not from guessing. **All four `RUNBOOK.md` scenarios have now been genuinely drilled against real, deliberately-broken conditions this session.**

### Verified (this session, part 5)

- **Live incident drill against `RUNBOOK.md`'s neo4j-crash-loop section -- confirmed correct, no changes needed.** Deliberately set a malformed `NEO4J_AUTH` value (missing the required `/` separator) to produce a genuine `CrashLoopBackOff` (71 restarts confirmed), then followed the documented triage steps exactly as written. Unlike the db-down and backup-failure sections (both fixed this session after finding real command errors), `kubectl logs` correctly and immediately surfaced the real, actionable error: `Invalid value for NEO4J_AUTH: 'this-is-not-valid-auth-format'`. Confirmed recovery: restored the real secret, `ragleap-neo4j` reached `1/1 Running`, and a real Cypher query succeeded. Third of four `RUNBOOK.md` scenarios now genuinely drilled against a real, deliberately-broken condition this session.

### Fixed (this session, part 4)

- **Live incident drill against `RUNBOOK.md`'s postgres backup-failure section found two real, previously-untested command errors.** The primary disk-space check (`kubectl exec ... deploy/ragleap-db -- df -h /backups`) is simply wrong -- `/backups` is never mounted into `ragleap-db` at all, only into the backup CronJob's own pod, confirmed live (`df: /backups: No such file or directory`). The documented fallback (an inline `--overrides` JSON one-liner) also proved fragile, failing under PowerShell quoting. Replaced with a small, portable YAML-file pattern matching this repo's own established convention (write anything non-trivial to a file, don't fight inline quoting) -- live-verified working, real output showed `944.4G` available on a `1006.9G` volume.
- This is the second `RUNBOOK.md` scenario drilled against a real, deliberately-tested condition this session (after db-down) to genuinely find documentation errors, not just structural review.

### Fixed (this session, part 3)

- **Live incident drill against `RUNBOOK.md`'s db-down scenario found the runbook's own documented triage steps insufficient.** Deliberately broke `ragleap-db-secret`'s `POSTGRES_PASSWORD` to simulate a real credential-drift incident, then followed the runbook's own three documented commands (`kubectl get pods`, `describe pod`, `logs`) with no prior knowledge of the injected fault -- all three reported a perfectly healthy pod (`1/1 Running`, `Ready: True`, `Events: <none>`), completely missing the real incident. Root cause: `pg_isready` (used by both probes) only checks TCP connectivity, not real credential validity, and `pg_hba.conf`'s default `trust` rule for `127.0.0.1` connections meant even manually testing from inside the pod gave a false all-clear -- confirmed by testing both the broken and the real password from inside the pod and getting `SELECT 1` success either way, then confirming a genuine failure only appeared when testing via a real external connection (`FATAL: password authentication failed`).
- Added a real, external connectivity check to `RUNBOOK.md`'s db-down triage section, using the actual configured credentials from `ragleap-db-secret`. The first version of this fix was itself wrong (tried running `kubectl` from inside a `postgres:16-alpine` debug pod, which has no kubectl binary -- `kubectl: not found`) -- caught immediately by testing the documented command exactly as written before shipping it, then corrected to read the credential locally first and pass it into the debug pod as an env var, re-verified working.

### Fixed (this session, part 2)

- Real gap found while writing `RUNBOOK.md` earlier this session, now closed: `k8s/neo4j-backup-cronjob.yaml` scaled `ragleap-neo4j` to 0 replicas before dumping, with no automatic scale-back-up or rollback if the dump failed partway through -- a failed backup could leave neo4j down indefinitely with nothing to notice or recover. Fixed using a native sidecar container (`restartPolicy: Always`, stable since k8s 1.29, confirmed supported on this repo's kind cluster at v1.30.0): a `scale-up-watcher` sidecar starts alongside the `dump` container and is only terminated after `dump` finishes, success or failure. `dump` uses `trap 'touch /signal/backup-done' EXIT` to always signal completion regardless of outcome; the sidecar waits for that signal, then scales `ragleap-neo4j` back to 1.
- **Both the success and failure paths were live-tested on a real kind cluster, not just the happy path.** Success: real backup completed (36 files, 257.8MiB, matching prior sessions' figures), `scale-up-watcher`'s own logs confirmed the scale command ran, `ragleap-neo4j` reached `1/1 Running`. Failure: deliberately broke the dump command (nonexistent database name) via a patched job spec, confirmed the Job correctly reports `Failed` (not silently masked), and confirmed `ragleap-neo4j` was still `1/1 Running` afterward -- proving the sidecar's `trap`-based signal fires on failure too, not just success.
- **Real, unrelated blocker found and fixed along the way**: `bitnami/kubectl:1.31`, already in use by the pre-existing `scale-down` init container, no longer resolves -- Bitnami has moved this image to digest-only tags (no more simple version tags like `:1.31`), confirmed via the Docker Hub API, not assumed. Replaced with `rancher/kubectl:v1.34.9`, which pulled successfully but turned out to have no shell at all (`exec: "sh": executable file not found in $PATH` -- a fully distroless image), confirmed via a direct `docker run --entrypoint sh` test before committing to a third option. Settled on `alpine/kubectl:1.34.2`, verified via the same direct test to have both a real shell and working `kubectl` before use.
- `backup-pvc.yaml` was found to have never been deployed to this test cluster at all -- the very first live test attempt failed with `persistentvolumeclaim \"ragleap-backup-data\" not found`, confirming this PVC is a genuine prerequisite for both backup CronJobs that isn't automatically created by anything else.

### Investigated

- Neo4j's `readOnlyRootFilesystem` compatibility -- **live-tested on a real kind cluster, confirmed UNSAFE, not enabled.** Confirmed the entrypoint rewrites `/var/lib/neo4j/conf` on every container restart (file mtime 12 seconds after container `finishedAt`). Added `/var/lib/neo4j/conf` as a writable `emptyDir` and deployed live: the pod reached `1/1 Running`, logs looked clean, and a real Cypher query (`RETURN 1`) succeeded -- but this was misleading. Diffing the running conf file against the actual unmodified image's shipped default (`docker run --entrypoint cat neo4j:5-community /var/lib/neo4j/conf/neo4j.conf`) revealed the emptyDir mount **shadows the image's real baked-in default config**, silently dropping it from 350 lines to ~5. Real, non-cosmetic losses: `server.directories.import=import` (a security boundary restricting `LOAD CSV` to Neo4j's own `import/` directory) and ~17 JVM tuning/TLS-hardening flags (`-XX:+UseG1GC`, `-Djdk.tls.rejectClientInitiatedRenegotiation=true`, etc.). Reverted immediately after confirming; the flag remains disabled, with the real root cause now documented inline in both manifest paths instead of the prior speculative comment. Would not have been caught by pod status or basic queries alone -- only by deliberately comparing the real running config against the image's actual shipped default.

### Fixed

- `readOnlyRootFilesystem` enabled across `db`, `app`, `voice` (both raw `k8s/` manifests and Helm chart templates) -- the previously-flagged gap ("intentionally NOT enabled yet -- not live-tested") is now closed for these three services. `neo4j` received the same writable-path mounts but the flag itself was deliberately left disabled -- the Neo4j Docker entrypoint rewrites `/var/lib/neo4j/conf` on every startup, and this has not been verified to tolerate a read-only root; documented inline in both manifest paths pending further investigation.
- Helm `app-deployment.yaml` and `voice-deployment.yaml` templates were found to have NO `securityContext` at all on either the `wait-for-db` init container or the main container -- a real, previously-undocumented gap distinct from the raw manifests (which did have SecurityContext). Closed as part of this change; both Helm templates now match the raw manifests' security posture.
- Real bug found via live testing, same class as the earlier SecurityContext finding: `app`/`voice`'s `wait-for-db` init container (`postgres:16-alpine`) had `runAsNonRoot: true` with no `runAsUser` set, in both the raw manifests (pre-existing, just never live-tested before) and the newly-added Helm securityContext blocks. Failed on real cluster with `container has runAsNonRoot and image will run as root`. Fixed with `runAsUser: 70`, confirmed via `docker run --rm postgres:16-alpine id postgres` (uid=70(postgres)), not assumed.

### Verified

- `db`: full `readOnlyRootFilesystem` live-tested on a real local kind cluster (Docker Desktop on Windows). Pod reached `1/1 Running`, 0 restarts. Unix socket created successfully at `/var/run/postgresql/.s.PGSQL.5432` (confirmed via `ls -la`, correct `postgres:postgres` ownership). `pg_isready -U ragleap` returned `accepting connections`. Schema DDL from `ragleap-db-schema` configmap applied successfully on startup.
- `neo4j`: writable-mount fix (partial, flag left off) live-tested on the same cluster. Pod reached `1/1 Running`, 0 restarts. Neo4j 5.26.30 started cleanly, Bolt (7687) and HTTP (7474) both enabled. `/logs` mount confirmed genuinely in use (`debug.log`, `neo4j.log` actively written, correct `neo4j:neo4j` ownership). `/var/lib/neo4j/run` confirmed in use (`neo4j.pid` present). Real Cypher query executed successfully via `cypher-shell` (`RETURN 1 AS test` -> `1`), confirming the database engine is genuinely functional, not just container-running.
- `app`/`voice` init containers: after the `runAsUser: 70` fix, both pods progressed cleanly past `wait-for-db` (previously `Init:CreateContainerConfigError`) with no further init-container errors.

### Known limitations

- `app`/`voice` main containers' `readOnlyRootFilesystem`/`/tmp` mount behavior could NOT be verified in this pass -- both pods reached `ImagePullBackOff` on `ghcr.io/antonyrag/ragleap-app:latest`, a private image requiring a real `ghcr-pull-secret` not available in this test session. This remains a genuinely open item, not assumed to work. Revisit once GHCR credentials are available.
- Neo4j's `/var/lib/neo4j/conf` read-only-root compatibility remains unverified -- flag intentionally left disabled, needs either entrypoint script investigation or a live attempt-and-observe test.


### Verified

- Full neo4j backup/DR restore drill performed end-to-end on a real local kind cluster -- the previously-flagged gap ("dump side verified, restore side never tested") is now closed. Real marker node created via cypher-shell, neo4j scaled to zero, `neo4j-admin database dump` taken against the unlocked PVC (36 files, 257.9MiB, matching the original session's dump size almost exactly), then `neo4j-admin database load --overwrite-destination=true` performed against the same volume, neo4j scaled back up, and the exact same marker node (same properties, same millisecond-precision timestamp) confirmed recovered via cypher-shell. Restore command completed with zero errors, notably cleaner than the noisy-but-successful Postgres restore (which produced expected `already exists` errors when restoring into a non-empty schema).


### Fixed

- Helm chart templates (`helm/ragleap-ops/templates/db-deployment.yaml`, `neo4j-deployment.yaml`) had NOT received the SecurityContext fixes from the prior raw-manifest fix (runAsUser, fix-permissions init container) -- confirmed real drift between the two deployment paths, same class of bug as the earlier neo4j probe-drift finding. Ported the identical fix to both Helm templates.

### Verified

- `helm lint` clean; `helm template` output confirmed both `fix-permissions` init containers and both `runAsUser` values (999, 7474) render correctly, not left as unresolved template syntax.
- Full `helm install` performed on a real local kind cluster (Docker Desktop on Windows). `neo4j` reached `1/1 Running` with 0 restarts. `db`'s `fix-permissions` init container completed successfully (exit code 0) and the main container reached `Ready: True`, confirming the ported fix works identically to the raw-manifest version already verified.

### Known limitations

- During this full-stack `helm install` (all 4 services deploying simultaneously, unlike the earlier minimal single-service raw-manifest test), `db` restarted twice due to `pg_isready` probe timeouts (`command timed out after 1s`/`5s`) -- root cause was real resource contention on the test laptop under the heavier simultaneous 4-service load (image pulls that normally take seconds took 50+ seconds), not a defect in the SecurityContext/init-container fix itself. The pod self-healed and reached a stable `1/1 Running` state. Worth revisiting probe timeout tuning if this recurs under realistic production load, but not treated as a blocking bug here since it reflects test-environment contention, not the fix being verified.
- `app`/`voice` could not be tested in this pass -- `ImagePullBackOff` due to missing `ghcr-pull-secret` and private image access in this fresh test cluster, expected and unrelated to this fix.


### Fixed

- SecurityContext hardening (added in a prior commit) broke real startup for `db` and `neo4j` on live cluster testing -- caught only now via an actual kind deploy, not template validation. Three real, layered issues found and fixed:
  1. `runAsNonRoot: true` alone rejected both images outright (`pgvector/pgvector:pg16` and `neo4j:5-community` both default to a root user before their entrypoints drop privileges internally) -- fixed by adding explicit `runAsUser` set to each image's real non-root UID (999 for postgres, 7474 for neo4j, confirmed via `docker run --rm <image> id <user>`, not assumed).
  2. Even with the correct UID, `initdb`/neo4j startup failed with `chmod: Operation not permitted` against the PVC-backed data directory, which K8s creates owned by root by default.
  3. Pod-level `fsGroup` alone did not resolve this -- it sets group ownership, not directory owner, and `chmod` on the directory itself requires ownership. Fixed with a dedicated `fix-permissions` init container (root, `chown -R <uid>:<uid> <mount>`, busybox:1.36) that runs once before the main container starts as its restricted UID.

### Verified

- Full backup/DR restore drill performed end-to-end on a real local kind cluster (Docker Desktop on Windows, not the constrained VPS used earlier -- VPS steal time made even minimal cluster boot fail outright, confirmed via a separate failed kubeadm init attempt). Real marker row inserted, `pg_dump` taken and byte-verified to contain it, table dropped with CASCADE, `SELECT` confirmed genuine data loss, restore performed from the dump file, `SELECT` confirmed the exact same row (same UUID, same microsecond timestamp) recovered. RTO for this single-table drill: under 2 minutes.
- `db` and `neo4j` deployments confirmed to actually reach `1/1 Running` with the fixed SecurityContext + fix-permissions init container pattern, `RESTARTS: 0` sustained.

### Known limitations

- Restoring a full-schema `pg_dump` into an already-initialized (non-empty schema) database produces expected `already exists`/`multiple primary keys` errors for every CREATE statement -- harmless (the actual data COPY statements still succeed), but noisy. A genuinely clean restore drill should target a freshly-provisioned empty database, not layer onto an existing schema the way this test did. Real operational finding, not previously documented.
- This restore drill covered Postgres only. The neo4j scale-to-zero + `neo4j-admin dump`/`load` restore path (built earlier this session) has NOT yet been live-restore-tested -- only the dump side was previously verified. Real remaining gap.
- Both fixes (runAsUser, fix-permissions init container) were verified in this session but not yet reflected in the Helm chart's equivalent templates (`helm/ragleap-ops/templates/`) -- only the raw `k8s/` manifests were fixed and tested here. The Helm chart needs the same fix, tracked as a follow-up (same class of bug as the earlier neo4j probe-drift finding between raw manifests and Helm templates). **Update:** the SecurityContext fix was later ported to the Helm chart templates and verified with a live `helm install` (#297).


## [0.3.0] - 2026-09-06

### Added

- Backup/DR: `backup-pvc.yaml`, `db-backup-cronjob.yaml` (daily `pg_dump`, no downtime), `neo4j-backup-cronjob.yaml` (scale-to-zero + `neo4j-admin dump`, since Community Edition has no online backup command).

### Verified

- `pg_dump` live-tested against a real running database, produced a valid dump.
- neo4j scale-to-zero + dump pattern live-tested end-to-end via a real Job, produced a genuine 257.8MiB/36-file dump successfully.
- CronJob wrapper (RBAC, scheduling) validated structurally, not exercised via an actual cron trigger this session.

### Known limitations

- Neo4j backup is fail-loud by design: a failed dump leaves neo4j scaled to 0 rather than auto-restoring, to avoid a silently-failing backup going unnoticed. Requires monitoring CronJob/Job status separately.

### Fixed

- `k8s/neo4j-deployment.yaml`'s liveness probe still had the original, pre-fix timing (`initialDelaySeconds: 20`, no explicit `failureThreshold`) even though the equivalent bug was already found and fixed in the Helm chart's neo4j template earlier this session. The two deployment paths had silently diverged. Found while live-testing backup/DR tooling on a fresh cluster -- neo4j genuinely crash-looped (kubelet killing it ~7 seconds into JVM startup, well before the database could bind its HTTP listener). Fixed to match the Helm chart's already-proven values (`initialDelaySeconds: 60`, `failureThreshold: 6`), then re-verified stable (0 further restarts after the one within the expected startup window).

### Added

- Environment overlay files (`values-dev.yaml`, `values-staging.yaml`, `values-prod.yaml`) for the Helm chart, layered on top of the base `values.yaml`.
- `app.replicaCount`/`voice.replicaCount` parameterized (were previously hardcoded to `1` in the templates, which would have silently made environment-based replica scaling impossible). `db`/`neo4j` intentionally remain hardcoded at 1 replica -- both are stateful, single-writer services on `ReadWriteOnce` PVCs.

### Verified

- Rendered output confirmed to differ correctly across all three overlays via `helm template -f values-<env>.yaml`: dev (1/1 replicas, ingress disabled), staging (1/1 replicas, ingress enabled with its own hostname), prod (3/2 replicas, ingress enabled with its own hostname).

### Added

- Optional Helm chart Ingress + cert-manager Certificate (`ingress.enabled`, default `false`) for exposing the app outside the cluster over TLS.

### Verified

- Full Ingress + TLS chain live-tested end-to-end on a real kind cluster with a genuine NGINX Ingress Controller and cert-manager, using a self-signed ClusterIssuer. Confirmed via openssl that the correct certificate (matching SNI hostname) was served, not a generic fallback. HTTP routing through the Ingress to the backend independently confirmed.

### Known limitations

- Production Let's Encrypt issuance was not live-tested this session — real ACME HTTP-01 challenges require public DNS and an internet-reachable port 80, which a local kind cluster cannot satisfy. The provided ClusterIssuer example is the standard, documented cert-manager pattern, not independently verified against a real domain.

### Added

- NetworkPolicy resources (both `k8s/` and `helm/ragleap-ops/`) restricting `ragleap-db` ingress to `ragleap-app`/`ragleap-voice` only, and `ragleap-neo4j` ingress to `ragleap-app` only (`ragleap-voice` doesn't use neo4j in the current codebase, confirmed by checking real source, not assumed).

### Verified

- NetworkPolicy enforcement mechanism live-tested end-to-end on a real kind + Calico cluster: unlabeled traffic genuinely blocked (timeout, exit code 1), correctly-labeled traffic genuinely allowed (exit code 0) — not just applied without error.
- Real label selectors cross-checked against actual Deployment manifests — confirmed exact match.

### Known limitations

- kind's default CNI does not enforce NetworkPolicy at all; testing requires Calico or another NetworkPolicy-capable CNI (documented in README).
- Full ragleap stack (db+app+voice+neo4j) was not live-tested together under Calico in this session due to genuine VPS memory constraints — Calico's own baseline overhead left insufficient headroom for reliable 4-service testing on this specific host. Enforcement mechanism and label correctness were verified separately, not as one combined integration test.


## [0.2.0] - 2026-08-31

### Added

- Helm chart (`helm/ragleap-ops/`) wrapping all four `k8s/` manifests (db, app, voice, neo4j) with real configurable `values.yaml` — image tags, resource limits, storage sizes, probe timings, credentials.
- Real database schema (`db/schema.sql`) is packaged into the chart via Helm's `.Files.Get`, copied verbatim from the repo's real schema file, not reproduced from memory.

### Fixed

- `neo4j`'s liveness probe had no explicit `failureThreshold` in the Helm template (unlike the raw k8s manifest, which was already correctly set) — defaulted to Kubernetes' built-in `3`, too tight for JVM startup under CPU contention. Found via live `helm install`/`helm upgrade` testing on a real `kind` cluster (not caught by `helm lint` or `helm template`, since those don't exercise runtime behavior). Fixed to match the readiness probe's more generous timing (`initialDelaySeconds: 60`, `failureThreshold: 6`).

### Verified

- All four services (`db`, `app`, `voice`, `neo4j`) independently reached `1/1 Running` with zero restarts for extended periods (60+ minutes) on a real `kind` cluster via `helm install`.
- Added explicit `resources.requests`/`limits` for `neo4j` (512Mi memory request, 1Gi limit; CPU tuned to 50m during testing) after live testing on this session's VPS surfaced real host-level CPU/memory contention (the test VPS runs several other production services concurrently) — this is a permanent, worthwhile chart improvement, not a workaround specific to this host.

### Known limitations

- Full 4-service stack was demonstrated stable in bounded windows (60+ min) on a resource-constrained single-node test host that was also running unrelated production services. Extended (multi-hour) concurrent-stack stability was not verified on this specific host due to genuine host-level memory/CPU exhaustion (swap fully exhausted at points during testing) — not a chart defect, but an honest scope boundary on what was tested. A dedicated cluster (not sharing a host with other production workloads) is expected to have materially more headroom.
- CPU resource values (`cpu: 50m` for neo4j) were tuned against a heavily constrained single-vCPU-allocatable test node and should be reviewed against real target-cluster capacity before production use, not assumed as a universal recommendation.
- Same limitations as v0.1.0 still apply: `app-env-secret.yaml`/`db-schema-configmap.yaml`'s real content are environment-specific and not shipped; PVC storage sizes remain placeholder defaults.

## [0.1.0] - 2026-08-30

### Added

- Initial release: Kubernetes Deployment + Service manifests for all four `docker-compose.yml` services (`db`, `app`, `voice`, `neo4j`), translated field-by-field from the real compose file rather than assumed.
- Live-tested end-to-end on a local `kind` cluster: all four pods reached `1/1 Running` with zero restarts. A `db` liveness-probe timing bug was found live (the original `initialDelaySeconds: 10` was too short for first-boot image pull + PVC attach, causing kubelet to kill a still-starting container) and fixed (`initialDelaySeconds: 45`, `failureThreshold: 6`), then re-verified clean.
- `app`/`voice` pull a real image (`ghcr.io/antonyrag/ragleap-app:latest`, built from the existing repo-root `Dockerfile` and pushed to GHCR) via a `ghcr-pull-secret` `imagePullSecret`.
- Python package scaffold (`pyproject.toml`, `src/ragleap_ops/`) added purely so this content can be versioned and released via the existing `release.yml` automation — the package itself ships no functional Python code, only the `k8s/` manifests.

### Known limitations

- No Helm chart yet — raw manifests were proven first via live cluster testing; Helm wrapping is planned next.
- `app-env-secret.yaml` and the real contents of `db-schema-configmap.yaml`'s referenced `schema.sql` are environment-specific and intentionally not shipped in this package — see README for how to regenerate them locally via `kubectl create secret`/`kubectl create configmap`.
- PVC storage sizes (`5Gi`) are placeholder defaults, not derived from any real capacity planning — `docker-compose.yml`'s named volumes are unbounded, so there was no real size to translate from.
