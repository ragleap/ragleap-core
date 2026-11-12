# ragleap-observability

Metrics, logs, and alerting for RagLeap Core -- Prometheus, Grafana,
Loki, and AlertManager, wired against the real db/app/voice/neo4j
services.

## Status

Prometheus + `postgres_exporter`, Grafana, Loki + Promtail, and
AlertManager are all built and live-verified end-to-end against a
real cluster:

- `postgres_exporter` connects to `ragleap-db` using a read-only
  monitoring role; `/metrics` returns real, non-zero PostgreSQL
  statistics (`pg_up 1`, live `pg_stat_database` values).
- Grafana's Prometheus and Loki datasources are both confirmed live
  through Grafana's own datasource proxy, not just "pod is Running."
- Loki + Promtail log shipping is proven end-to-end: 21+ real log
  streams confirmed in Loki with correct `namespace`/`pod`/`container`
  labels, spanning nearly every pod in the cluster. See CHANGELOG for
  the full diagnostic history -- several real, non-obvious bugs (a
  cross-namespace DNS lookup, a Kubernetes service-discovery `__path__`
  resolution issue, and a YAML document-boundary corruption) were
  found and fixed to get here.
- AlertManager is wired end-to-end to Prometheus and live-verified:
  Prometheus's own `/api/v1/alertmanagers` confirms it as a genuinely
  registered target, and a real first alert rule (`PostgresExporterDown`)
  loads correctly. **Optional Slack and email receivers are supported but
  off by default** (`alertmanager.receivers.*` in `values.yaml`);
  credentials come from a Secret you create, never from values. Until
  you enable one, alerts go to a placeholder webhook and nothing notifies
  a human. Firing and resolved delivery were verified end to end on a live
  cluster against a Slack-compatible stand-in; a real Slack/email
  send has not yet been confirmed.
- Images pinned to current releases (AlertManager v0.34.1, Loki 3.6.17,
  Grafana 12.4.15, Prometheus v3.13.4). The Trivy counts are point-in-time
  measurements (findings with a fix available fell from 383 to 14 across
  these four images on 2026-10-03, with the earlier Grafana pin); the weekly
  scan in .github/workflows/trivy.yml reports the current ones. PostgreSQL exporter
  is on v0.20.1. Promtail is end of life (see "Log shipping" below), so it
  is not upgraded.
- Least-privilege hardening, verified on a live cluster: Prometheus,
  Grafana, Loki, AlertManager and postgres-exporter each run under their
  own zero-permission ServiceAccounts with no API token mounted, and
  Promtail's ClusterRole grants `pods` only. Grafana's admin password is
  configurable (`grafana.admin.password` or `grafana.admin.existingSecret`).

SLO/SLI dashboards are not yet built -- correctly sequenced after
alerting has proven reliable, per the build order below.

## Planned build order

1. Prometheus + exporters (postgres_exporter for db) -- done,
   live-verified. neo4j's exporter remains disabled pending
   independent verification, see "Known open items".
2. Grafana, provisioned dashboards-as-code, verified against real
   flowing data -- done, live-verified.
3. Loki + log shipping -- done, live-verified end-to-end.
4. AlertManager -- done, wired and live-verified. Slack/email
   receivers available but off by default; see "Known open items".
5. SLO/SLI dashboards, only after real data has accumulated for days,
   not minutes

## Design principles

- RagLeap-specific scoping, same honest choice `ragleap-ops` made for
  itself -- not a generic monitoring tool
- Every claim live-verified against a real cluster or explicitly
  labeled unverified, same discipline as every other package here

## Known open items

- **Neo4j Prometheus support is unverified and conflicting in the wild.**
  Neo4j's own KB and a working xk6-neo4j example show
  `metrics.prometheus.enabled=true` configured successfully against
  Neo4j Community 4.4.x. A separate monitoring vendor's own
  compatibility notes claim Community Edition is unsupported for their
  specific collector. `neo4jExporter.enabled` defaults to `false` in
  `values.yaml` until this is live-verified against the real
  `neo4j:5-community` image in this repo's own `kind` cluster.
- Loki's retention (`168h` / 7 days) is unverified for real
  storage-sizing needs; flagged in `values.yaml` as a placeholder.
- **AlertManager receivers are off by default, and a real Slack/email
  send is unconfirmed** (delivery is verified against a stand-in). Enable `alertmanager.receivers.slack` and/or `.email` and
  create the Secret named in `values.yaml`. Until then alerts are routed
  and grouped but go to a placeholder webhook. PagerDuty is not
  supported yet.
- AlertManager's state (silences, notification log) uses `emptyDir`,
  not a PVC -- lost on pod restart.
- A recurring log-shipping health check remains unbuilt.
- **Grafana's admin password defaults to a placeholder** when no option is
  set (the install prints a warning). On an existing release, upgrading will
  not change it; use `grafana cli admin reset-admin-password` inside the pod,
  and note this leaves the chart's Secret out of sync.
- No canary or blue-green deployment strategy exists in this project
  -- every Deployment uses the Kubernetes default `RollingUpdate` or,
  for Prometheus specifically, an explicit `Recreate` (required by its
  single-writer TSDB storage). Correctly sequenced after GitOps
  tooling (ArgoCD/Flux), which has not been started.

## Log shipping: Promtail or Alloy

Promtail is the default. Grafana has ended Promtail and replaced it with Grafana Alloy, which this chart can run instead (off by default):

```bash
helm upgrade ragleap-observability ./ragleap-observability --set alloy.enabled=true --set promtail.enabled=false
```

- Enabling both fails at render time, because every line would be shipped twice.
- Alloy keeps its read positions in `alloy.dataHostPath` (default `/var/lib/ragleap-alloy`) on each node. That is the only writable host path in this chart, and removing Alloy leaves the directory behind.
- Alloy runs as root, because the node's pod log files are root-owned. Pull its image onto the nodes first to keep the gap between shippers short.
- Tested on one `kind` node: a marker stream stayed in order, without gaps or repeats, across a switch to Alloy and back. A fresh Alloy replays existing files, and Loki rejects the entries that are too old, which Alloy logs as `entry too far behind`.

## Availability SLO alerts (0.8.0, opt-in)

Set `slo.enabled=true` to add fast-burn and slow-burn alerts on the Prometheus `up` metric for the jobs in `slo.availability.jobs`. The 99.5% target (`slo.availability.errorBudget: 0.005`) is a placeholder, not a measured figure.

- Verified: the rule logic, with `promtool` unit tests (`tests/promtool/run.sh`).
- Verified on a real Prometheus (v3.13.4, one container, no Kubernetes, 15 s scrape interval; see `tests/live/`): fast burn, `PostgresExporterDown` and `RagleapTargetAbsent` fired and cleared as expected.
- Verified on a throwaway kind cluster with the real chart and a stand-in receiver (workflow `obs-k8s-test`, run 39112387967, 2026-10-18; the Kubernetes version was not pinned in that run): the four rules loaded and Prometheus had an active AlertManager; a healthy system fired nothing; a stopped exporter fired `RagleapAvailabilityFastBurn` (after 190 s) and `PostgresExporterDown` (after 171 s); both reached the receiver and the resolved notification arrived; a removed target fired `RagleapTargetAbsent` (after 581 s) and reached the receiver.
- Not verified: slow-burn firing, a real Slack or email send, and any Kubernetes version or multi-node setup beyond that one run.
- Limits: this measures target reachability, not user experience, because the app exports no request metrics yet. A target that disappears entirely does not trigger the burn-rate alerts; from 0.8.2 a separate `RagleapTargetAbsent` alert covers it (rule logic tested, and its firing and delivery observed on kind in run 39112387967).
