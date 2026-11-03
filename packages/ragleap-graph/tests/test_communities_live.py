"""Live test of the community methods against a real Neo4j. Runs only when NEO4J_URI, NEO4J_USER
and NEO4J_PASSWORD are set; works in its own namespace and deletes everything it wrote."""
import os
import uuid

import pytest

from ragleap_graph import GraphConfig, GraphIndex

NEO4J_URI = os.environ.get("NEO4J_URI")
NEO4J_USER = os.environ.get("NEO4J_USER")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")
HAS_LIVE_NEO4J = bool(NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD)


@pytest.mark.skipif(not HAS_LIVE_NEO4J, reason="No live Neo4j credentials in environment")
def test_communities_end_to_end_on_a_real_neo4j():
    ns = f"ragleap_graph_comm_{uuid.uuid4().hex}"
    g = GraphIndex(config=GraphConfig(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD))
    assert g.driver is not None
    calls = []

    def summarizer(info):
        calls.append(info["members"])
        return "Summary of " + ", ".join(info["display_names"])

    try:
        g.upsert_document("d1", "D1", [{"text": "Acme Corp and Globex Corp and Initech Corp."}], namespace=ns)
        g.upsert_document("d2", "D2", [{"text": "Acme Corp and Globex Corp and Initech Corp again."}], namespace=ns)
        g.upsert_document("d3", "D3", [{"text": "Wayne Corp and Stark Corp and Umbrella Corp."}], namespace=ns)
        g.upsert_document("d4", "D4", [{"text": "Wayne Corp and Stark Corp and Umbrella Corp again."}], namespace=ns)
        g.upsert_document("d5", "D5", [{"text": "Initech Corp and Wayne Corp."}], namespace=ns)

        detected = g.detect_communities(namespace=ns)
        assert [c["members"] for c in detected] == [
            ["globex corp", "initech corp", "acme corp"][::-1][::-1] and sorted(["acme corp", "globex corp", "initech corp"]),
            sorted(["stark corp", "umbrella corp", "wayne corp"]),
        ]

        first = g.refresh_communities(namespace=ns)
        assert first["success"] is True and (first["communities"], first["created"], first["kept"]) == (2, 2, 0)

        found = g.find_communities(["Acme Corp"], namespace=ns, include_members=True)
        assert len(found) == 1 and found[0]["matched_entities"] == 1 and found[0]["size"] == 3
        assert sorted(found[0]["members"]) == ["Acme Corp", "Globex Corp", "Initech Corp"]
        assert found[0]["summary"] is None

        second = g.refresh_communities(namespace=ns, summarizer=summarizer)
        assert (second["created"], second["kept"], second["removed"], second["summarized"]) == (0, 2, 0, 2)
        assert len(calls) == 2
        assert g.find_communities(["Acme Corp"], namespace=ns)[0]["summary"].startswith("Summary of ")

        third = g.refresh_communities(namespace=ns, summarizer=summarizer)
        assert (third["created"], third["kept"], third["removed"], third["summarized"]) == (0, 2, 0, 0)
        assert len(calls) == 2

        wayne_before = g.find_communities(["Wayne Corp"], namespace=ns)[0]
        g.upsert_document("d6", "D6", [{"text": "Acme Corp and Hooli Corp and Globex Corp and Initech Corp."}], namespace=ns)
        fourth = g.refresh_communities(namespace=ns, summarizer=summarizer)
        assert (fourth["created"], fourth["kept"], fourth["removed"], fourth["summarized"]) == (1, 1, 1, 1)
        wayne_after = g.find_communities(["Wayne Corp"], namespace=ns)[0]
        assert wayne_after["community_id"] == wayne_before["community_id"]
        assert wayne_after["summary"] == wayne_before["summary"]
        grown = g.find_communities(["Hooli Corp"], namespace=ns, include_members=True)[0]
        assert grown["size"] == 4 and grown["summary"].startswith("Summary of ")
        assert len(calls) == 3
        assert len(g.find_communities(namespace=ns)) == 2

        other = g.find_communities(["Acme Corp"], namespace=ns + "_other")
        assert other == []
    finally:
        with g.driver.session() as session:
            session.run("MATCH (n {namespace: $namespace}) DETACH DELETE n", namespace=ns)
        g.close()
