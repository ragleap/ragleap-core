"""ragleap-observability: metrics, logs, and alerting for RagLeap Core.

This package ships Helm chart configuration wiring Prometheus (with
exporters for db/neo4j), Grafana (provisioned dashboards-as-code),
Loki (log aggregation), and AlertManager (routed to Slack/PagerDuty)
against RagLeap Core's own 4 services -- same RagLeap-specific scoping
choice ragleap-ops made for itself, not a generic tool.

This is the prerequisite for AIOps per the DevOps maturity roadmap:
anomaly detection and auto-remediation need a real metrics pipeline to
operate on, which doesn't exist until this package ships.

It contains almost no importable Python -- its purpose is to let the
existing PyPI/git-tag release automation version and ship the helm/
chart alongside this src/ tree, matching the release discipline of
ragleap-ops, ragleap-rag, ragleap-graph, and ragleap-vectorstores.
"""

__version__ = "0.8.5"
