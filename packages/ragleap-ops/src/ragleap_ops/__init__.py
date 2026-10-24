"""ragleap-ops: Kubernetes deployment tooling for RagLeap Core.

This package ships k8s/ Deployment and Service manifests for all four
docker-compose services (db, app, voice, neo4j), translated from the
real docker-compose.yml and live-tested end-to-end on a kind cluster.

It contains almost no importable Python — its purpose is to let the
existing PyPI/git-tag release automation version and ship the k8s/
manifests alongside this src/ tree, matching the release discipline of
ragleap-rag, ragleap-graph, and ragleap-vectorstores.
"""

__version__ = "0.6.2"
