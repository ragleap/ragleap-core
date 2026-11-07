"""Temporal relation edges against a real Neo4j (needs NEO4J_URI; own namespace, removed afterwards)."""
import os
import uuid
from datetime import timedelta

import pytest

from ragleap_graph import GraphConfig, GraphIndex

pytestmark = pytest.mark.skipif(not os.environ.get("NEO4J_URI"), reason="needs NEO4J_URI")


def test_open_close_reopen_and_as_of():
    ns = "ragleap_graph_temporal_" + uuid.uuid4().hex
    g = GraphIndex(GraphConfig(uri=os.environ["NEO4J_URI"], user=os.environ.get("NEO4J_USER", "neo4j"),
                               password=os.environ.get("NEO4J_PASSWORD", "")), temporal=True)
    assert g.driver is not None
    ck, ck2 = "edge-" + ns, "edge2-" + ns

    def run(q, **p):
        with g.driver.session() as s:
            return [r.data() for r in s.run(q, **p)]

    def refresh(key, relation_type="PARTNERED_WITH"):
        run(GraphIndex._RELATES_AS_REFRESH_TEMPORAL, namespace=ns, subject="acme corp", relation_type=relation_type,
            object="globex corp", user_id="", relates_as_composite_key=key)

    def edge(key):
        return run("MATCH ()-[r:RELATES_AS {composite_key: $k}]->() "
                   "RETURN r.valid_from AS vf, r.valid_to AS vt, r.weight AS w", k=key)

    def support(on):
        run("MATCH (rw:RelationWeight {namespace: $ns}) DELETE rw", ns=ns)
        if on:
            run("CREATE (:RelationWeight {composite_key: $k, namespace: $ns, document_id: 'd1', subject: 'acme corp', "
                "relation_type: 'PARTNERED_WITH', object: 'globex corp', user_id: '', weight: 2.0})", k="rw-" + ns, ns=ns)

    def found(**kw):
        return g.find_relations("Acme Corp", namespace=ns, **kw)

    try:
        for name in ("acme corp", "globex corp"):
            run("CREATE (:Entity {composite_key: $k, name: $n, display_name: $d, namespace: $ns, user_id: ''})",
                k=ns + name, n=name, d=name.title(), ns=ns)
        support(True)
        refresh(ck)
        rows = edge(ck)
        assert len(rows) == 1 and rows[0]["vt"] is None and rows[0]["vf"] is not None and rows[0]["w"] == 2.0
        assert len(found()) == 1
        first_from = rows[0]["vf"]

        support(False)
        refresh(ck)
        rows = edge(ck)
        assert len(rows) == 1 and rows[0]["vt"] is not None and rows[0]["vf"] == first_from
        vf, vt = rows[0]["vf"].to_native(), rows[0]["vt"].to_native()
        assert vt > vf
        assert found() == []
        assert len(found(as_of=vf + (vt - vf) / 2)) == 1
        assert found(as_of=vt) == []
        assert found(as_of=vf - timedelta(seconds=1)) == []
        refresh(ck)  # closing an already closed edge keeps the first close time
        assert edge(ck)[0]["vt"].to_native() == vt

        support(True)
        refresh(ck)
        rows = edge(ck)
        assert rows[0]["vt"] is None and rows[0]["vf"].to_native() >= vt
        assert len(found()) == 1
        assert found(as_of=vf + (vt - vf) / 2) == []  # one interval per edge: the earlier gap is not remembered

        support(False)
        refresh(ck2, "OTHER")  # a relation that never had an edge is closed at once
        rows = edge(ck2)
        assert len(rows) == 1 and rows[0]["vf"] == rows[0]["vt"] and rows[0]["w"] == 0.0
        assert found(relation_type="OTHER") == []
        print("live temporal: open, close, reopen, as_of and phantom close checked")
    finally:
        run("MATCH (n) WHERE n.namespace = $ns DETACH DELETE n", ns=ns)
        g.driver.close()
