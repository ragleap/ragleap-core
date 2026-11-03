"""Tests for the GraphIndex community methods: detect_communities, refresh_communities and
find_communities. A fake driver records the exact statements and parameters sent to Neo4j and
returns scripted rows, so the bookkeeping (what is created, kept, removed and summarized) is
checked without a database. The live test is in test_communities_live.py."""
import pytest

from ragleap_graph import GraphConfig, GraphIndex, _composite_key
from ragleap_graph.communities import members_hash


def squash(text):
    return "".join(text.split())


GOLDEN = {
    "constraint": squash("CREATE CONSTRAINT community_composite_key IF NOT EXISTS FOR (c:Community) REQUIRE c.composite_key IS UNIQUE"),
    "edges": squash("""
        MATCH (a:Entity {namespace: $namespace})-[r:CO_OCCURS_WITH]-(b:Entity {namespace: $namespace})
        WHERE a.name < b.name
          AND coalesce(a.user_id, '') = $user_id
          AND coalesce(b.user_id, '') = $user_id
        RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
        LIMIT $limit
    """),
    "relation_edges": squash("""
        MATCH (a:Entity {namespace: $namespace})-[r:RELATES_AS]-(b:Entity {namespace: $namespace})
        WHERE a.name < b.name
          AND coalesce(a.user_id, '') = $user_id
          AND coalesce(b.user_id, '') = $user_id
        RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS weight
        LIMIT $limit
    """),
    "existing": squash("""
        MATCH (c:Community {namespace: $namespace})
        WHERE coalesce(c.user_id, '') = $user_id
        RETURN c.composite_key AS key, c.summary IS NOT NULL AS has_summary
    """),
    "merge": squash("""
        MERGE (c:Community {composite_key: $composite_key})
        ON CREATE SET c.created_at = datetime(), c.namespace = $namespace, c.user_id = $user_id,
            c.members_hash = $members_hash
        SET c.size = $size, c.internal_weight = $internal_weight, c.resolution = $resolution,
            c.updated_at = datetime()
    """),
    "members": squash("""
        MATCH (c:Community {composite_key: $composite_key})
        UNWIND $members AS member_name
        MATCH (e:Entity {name: member_name, namespace: $namespace})
        WHERE coalesce(e.user_id, '') = $user_id
        MERGE (e)-[:IN_COMMUNITY]->(c)
    """),
    "delete": squash("""
        MATCH (c:Community {composite_key: $composite_key})
        DETACH DELETE c
    """),
    "names": squash("""
        MATCH (e:Entity {namespace: $namespace})
        WHERE e.name IN $members AND coalesce(e.user_id, '') = $user_id
        RETURN e.name AS name, e.display_name AS display_name
    """),
    "relations": squash("""
        MATCH (s:Entity {namespace: $namespace})-[r:RELATES_AS]->(o:Entity {namespace: $namespace})
        WHERE s.name IN $members AND o.name IN $members
          AND coalesce(s.user_id, '') = $user_id AND coalesce(o.user_id, '') = $user_id
        RETURN s.display_name AS subject, r.relation_type AS relation_type, o.display_name AS object,
               coalesce(r.weight, 1.0) AS weight
        ORDER BY weight DESC
        LIMIT $limit
    """),
    "summary": squash("""
        MATCH (c:Community {composite_key: $composite_key})
        SET c.summary = $summary, c.summary_updated_at = datetime()
    """),
    "list": squash("""
        MATCH (c:Community {namespace: $namespace})
        WHERE coalesce(c.user_id, '') = $user_id
        RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
               c.summary AS summary, 0 AS matched_entities
        ORDER BY member_count DESC, community_id ASC
        LIMIT $limit
    """),
    "by_entity": squash("""
        MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community)
        WHERE e.namespace = $namespace
          AND c.namespace = $namespace
          AND coalesce(e.user_id, '') = $user_id
          AND coalesce(c.user_id, '') = $user_id
          AND e.name IN $entity_names
        RETURN c.composite_key AS community_id, c.size AS member_count, c.internal_weight AS internal_weight,
               c.summary AS summary, count(DISTINCT e) AS matched_entities
        ORDER BY matched_entities DESC, member_count DESC, community_id ASC
        LIMIT $limit
    """),
    "member_names": squash("""
        MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community {composite_key: $community_id})
        WHERE coalesce(e.user_id, '') = $user_id
        RETURN e.display_name AS entity_name
        ORDER BY entity_name
        LIMIT $limit
    """),
}
KIND_BY_TEXT = {text: name for name, text in GOLDEN.items()}
assert len(KIND_BY_TEXT) == len(GOLDEN)

A = ["a1", "a2", "a3"]
B = ["b1", "b2", "b3"]
TRIANGLES = [
    ("a1", "a2", 1.0), ("a1", "a3", 2.0), ("a2", "a3", 3.0),
    ("b1", "b2", 1.0), ("b1", "b3", 1.0), ("b2", "b3", 1.0),
    ("a3", "b1", 1.0),
]
CO_MARKER = "[r:CO_OCCURS_WITH]-(b:Entity"
REL_MARKER = "[r:RELATES_AS]-(b:Entity"


def edge_rows(edges):
    return [{"a": a, "b": b, "weight": float(w)} for a, b, w in edges]


BASE = [(CO_MARKER, edge_rows(TRIANGLES))]


class FakeSession:
    def __init__(self, drv):
        self.drv = drv

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.drv.calls.append({"cypher": "".join(query.split()), "params": params})
        if self.drv.fail_on and self.drv.fail_on in query:
            raise RuntimeError("boom")
        for marker, rows in self.drv.script:
            if marker in query:
                return [dict(r) for r in rows]
        return []


class FakeDriver:
    def __init__(self, script, fail_on=None):
        self.script = script
        self.fail_on = fail_on
        self.calls = []

    def session(self):
        return FakeSession(self)

    def close(self):
        pass


class Rec:
    def __init__(self):
        self.events = []

    def log(self, **kwargs):
        self.events.append(kwargs)

    def close(self):
        pass


def make_index(script, fail_on=None, driver=True):
    g = GraphIndex(config=GraphConfig(uri="bolt://localhost:1", user="x", password="x"))
    assert g.driver is None
    audit = Rec()
    g._audit = audit
    if driver:
        g.driver = FakeDriver(script, fail_on)
    return g, audit


def kinds(g):
    return [KIND_BY_TEXT.get(c["cypher"], "UNKNOWN " + c["cypher"][:70]) for c in g.driver.calls]


def key_of(ns, uid, members):
    return _composite_key(ns, uid, "community", members_hash(members))


# ---- detect_communities ----

def test_detect_communities_reads_the_co_occurrence_edges():
    g, audit = make_index(BASE)
    result = g.detect_communities(namespace="ns")
    assert [c["members"] for c in result] == [A, B]
    assert kinds(g) == ["edges"]
    assert g.driver.calls[0]["params"] == {"namespace": "ns", "user_id": "", "limit": 200001}
    assert audit.events == [{
        "user_id": "", "namespace": "ns", "action": "detect_communities",
        "detail": {"edges": 7, "communities": 2, "resolution": 1.0},
    }]


def test_typed_relations_can_be_added_as_edges():
    g, audit = make_index(BASE + [(REL_MARKER, edge_rows([("a1", "b3", 0.5)]))])
    g.detect_communities(namespace="ns", include_relations=True)
    assert kinds(g) == ["edges", "relation_edges"]
    assert audit.events[0]["detail"]["edges"] == 8


def test_too_many_edges_raise_instead_of_returning_a_partial_graph():
    g, _ = make_index(BASE)
    with pytest.raises(ValueError, match="max_edges=5"):
        g.detect_communities(namespace="ns", max_edges=5)
    assert g.driver.calls[0]["params"]["limit"] == 6
    g2, _ = make_index(BASE)
    assert len(g2.detect_communities(namespace="ns", max_edges=7)) == 2


@pytest.mark.parametrize("kwargs", [
    {"resolution": 0}, {"resolution": float("nan")}, {"min_size": 0}, {"min_size": True},
    {"max_edges": 0}, {"max_edges": True}, {"max_edges": 1.5},
])
def test_bad_arguments_raise_even_without_a_driver(kwargs):
    g, _ = make_index([], driver=False)
    with pytest.raises(ValueError):
        g.detect_communities(**kwargs)
    with pytest.raises(ValueError):
        g.refresh_communities(**kwargs)


def test_without_a_driver_nothing_happens():
    g, audit = make_index([], driver=False)
    assert g.detect_communities() == []
    assert g.refresh_communities() == {
        "success": False, "communities": 0, "created": 0, "kept": 0, "removed": 0,
        "summarized": 0, "summary_errors": 0, "error": "Neo4j driver not available",
    }
    assert g.find_communities(["Acme Corp"]) == []
    assert audit.events == []


def test_a_failing_query_gives_an_empty_list_or_an_error_summary():
    g, _ = make_index(BASE, fail_on="CO_OCCURS_WITH")
    assert g.detect_communities(namespace="ns") == []
    g2, audit = make_index(BASE, fail_on="CO_OCCURS_WITH")
    result = g2.refresh_communities(namespace="ns")
    assert result["success"] is False and result["error"] == "boom" and result["created"] == 0
    assert audit.events == []
    g3, _ = make_index(BASE, fail_on="CREATE CONSTRAINT")
    assert g3.refresh_communities(namespace="ns")["error"] == "boom"
    g4, _ = make_index(BASE, fail_on="IN_COMMUNITY")
    assert g4.find_communities(["Acme Corp"]) == []


# ---- refresh_communities ----

def test_refresh_stores_new_communities_with_their_members():
    g, audit = make_index(BASE)
    result = g.refresh_communities(namespace="ns", user_id="u1")
    assert result == {
        "success": True, "communities": 2, "created": 2, "kept": 0, "removed": 0,
        "summarized": 0, "summary_errors": 0, "error": None,
    }
    assert kinds(g) == ["edges", "constraint", "existing", "merge", "members", "merge", "members"]
    calls = g.driver.calls
    key_a, key_b = key_of("ns", "u1", A), key_of("ns", "u1", B)
    assert calls[0]["params"] == {"namespace": "ns", "user_id": "u1", "limit": 200001}
    assert calls[1]["params"] == {}
    assert calls[2]["params"] == {"namespace": "ns", "user_id": "u1"}
    assert calls[3]["params"] == {
        "composite_key": key_a, "namespace": "ns", "user_id": "u1", "members_hash": members_hash(A),
        "size": 3, "internal_weight": 6.0, "resolution": 1.0,
    }
    assert calls[4]["params"] == {"composite_key": key_a, "namespace": "ns", "user_id": "u1", "members": A}
    assert calls[5]["params"]["composite_key"] == key_b and calls[5]["params"]["internal_weight"] == 3.0
    assert calls[6]["params"]["members"] == B
    assert audit.events == [{
        "user_id": "u1", "namespace": "ns", "action": "refresh_communities",
        "detail": {"communities": 2, "created": 2, "kept": 0, "removed": 0, "summarized": 0, "summary_errors": 0},
    }]


def test_refresh_stores_the_resolution_it_was_run_with():
    script = [(CO_MARKER, edge_rows([("a", "b", 10), ("c", "d", 10), ("b", "c", 1)]))]
    g, _ = make_index(script)
    result = g.refresh_communities(namespace="ns", resolution=2.0)
    assert result["created"] == 2
    assert g.driver.calls[3]["params"]["resolution"] == 2.0


def test_unchanged_communities_are_kept_old_ones_removed_and_only_new_ones_summarized():
    key_a, key_b = key_of("ns", "", A), key_of("ns", "", B)
    script = BASE + [("AS has_summary", [{"key": key_a, "has_summary": True}, {"key": "stale-key", "has_summary": True}])]
    g, audit = make_index(script)
    seen = []

    def summarizer(info):
        seen.append(info["members"])
        return "summary of " + info["community_id"][:6]

    result = g.refresh_communities(namespace="ns", summarizer=summarizer)
    assert result == {
        "success": True, "communities": 2, "created": 1, "kept": 1, "removed": 1,
        "summarized": 1, "summary_errors": 0, "error": None,
    }
    assert kinds(g) == ["edges", "constraint", "existing", "merge", "members", "merge", "members",
                        "delete", "names", "relations", "summary"]
    assert seen == [B]
    calls = g.driver.calls
    assert calls[7]["params"] == {"composite_key": "stale-key"}
    assert calls[10]["params"] == {"composite_key": key_b, "summary": "summary of " + key_b[:6]}
    assert audit.events[0]["detail"] == {"communities": 2, "created": 1, "kept": 1, "removed": 1, "summarized": 1, "summary_errors": 0}


@pytest.mark.parametrize("force, expected", [(False, [B]), (True, [A, B])])
def test_communities_without_a_summary_are_summarized_and_force_redoes_all(force, expected):
    key_a, key_b = key_of("ns", "", A), key_of("ns", "", B)
    script = BASE + [("AS has_summary", [{"key": key_a, "has_summary": True}, {"key": key_b, "has_summary": False}])]
    g, _ = make_index(script)
    seen = []
    result = g.refresh_communities(namespace="ns", summarizer=lambda info: seen.append(info["members"]) or "text", force=force)
    assert seen == expected
    assert (result["created"], result["kept"], result["summarized"]) == (0, 2, len(expected))


def test_the_summarizer_gets_members_names_strongest_pairs_and_relations():
    script = BASE + [
        ("e.display_name AS display_name", [{"name": "a1", "display_name": "A One"}, {"name": "a2", "display_name": "A Two"}]),
        ("AS relation_type", [{"subject": "A One", "relation_type": "WORKS_WITH", "object": "A Two", "weight": 2}]),
    ]
    g, _ = make_index(script)
    infos = []
    g.refresh_communities(namespace="ns", summarizer=lambda info: infos.append(info) or "text")
    first = infos[0]
    assert first["members"] == A and first["size"] == 3 and first["internal_weight"] == 6.0
    assert first["community_id"] == key_of("ns", "", A)
    assert first["display_names"] == ["A One", "A Two", "a3"]
    assert first["top_pairs"] == [
        {"a": "a2", "b": "a3", "weight": 3.0}, {"a": "a1", "b": "a3", "weight": 2.0}, {"a": "a1", "b": "a2", "weight": 1.0},
    ]
    assert first["relations"] == [{"subject": "A One", "relation_type": "WORKS_WITH", "object": "A Two", "weight": 2.0}]
    assert infos[1]["display_names"] == B
    names_call = [c for c in g.driver.calls if KIND_BY_TEXT.get(c["cypher"]) == "names"][0]
    assert names_call["params"] == {"namespace": "ns", "user_id": "", "members": A}


def test_max_summary_items_limits_pairs_and_relations():
    g, _ = make_index(BASE)
    infos = []
    g.refresh_communities(namespace="ns", summarizer=lambda info: infos.append(info) or "text", max_summary_items=2)
    assert len(infos[0]["top_pairs"]) == 2
    relation_call = [c for c in g.driver.calls if KIND_BY_TEXT.get(c["cypher"]) == "relations"][0]
    assert relation_call["params"] == {"namespace": "ns", "user_id": "", "members": A, "limit": 2}


@pytest.mark.parametrize("bad", ["", "   ", 5, None])
def test_a_summarizer_that_returns_nothing_usable_is_counted_not_raised(bad):
    g, _ = make_index(BASE)
    result = g.refresh_communities(namespace="ns", summarizer=lambda info: bad)
    assert result["success"] is True and result["summary_errors"] == 2 and result["summarized"] == 0
    assert "summary" not in kinds(g)


def test_a_summarizer_that_raises_is_counted_and_the_others_still_run():
    def summarizer(info):
        if info["members"] == A:
            raise RuntimeError("model down")
        return "fine"

    g, _ = make_index(BASE)
    result = g.refresh_communities(namespace="ns", summarizer=summarizer)
    assert (result["summarized"], result["summary_errors"], result["success"]) == (1, 1, True)
    assert kinds(g).count("summary") == 1


def test_summary_text_is_stripped_and_capped():
    g, _ = make_index(BASE)
    g.refresh_communities(namespace="ns", summarizer=lambda info: "  " + "x" * 30000)
    summary_calls = [c for c in g.driver.calls if KIND_BY_TEXT.get(c["cypher"]) == "summary"]
    assert len(summary_calls) == 2
    assert summary_calls[0]["params"]["summary"] == "x" * 20000


def test_refresh_argument_checks():
    g, _ = make_index(BASE)
    with pytest.raises(ValueError):
        g.refresh_communities(summarizer="not callable")
    for bad in (0, True, 1.5):
        with pytest.raises(ValueError):
            g.refresh_communities(summarizer=lambda info: "x", max_summary_items=bad)


# ---- find_communities ----

def test_find_communities_by_entity():
    row = {"community_id": "k1", "member_count": 3, "internal_weight": 6.0, "summary": "S", "matched_entities": 2}
    g, audit = make_index([("AS matched_entities", [row])])
    result = g.find_communities(["Acme Corp", "ACME CORP", "ab"], namespace="ns", user_id="u1", limit=5)
    assert result == [{"community_id": "k1", "size": 3, "internal_weight": 6.0, "summary": "S", "matched_entities": 2}]
    assert kinds(g) == ["by_entity"]
    assert g.driver.calls[0]["params"] == {"namespace": "ns", "user_id": "u1", "entity_names": ["acme corp"], "limit": 5}
    assert audit.events == [{
        "user_id": "u1", "namespace": "ns", "action": "find_communities",
        "detail": {"entity_names": ["acme corp"], "result_count": 1},
    }]


def test_find_communities_without_names_lists_the_largest():
    row = {"community_id": "k1", "member_count": 4, "internal_weight": None, "summary": None, "matched_entities": 0}
    g, audit = make_index([("AS matched_entities", [row])])
    result = g.find_communities(namespace="ns", limit=0)
    assert result == [{"community_id": "k1", "size": 4, "internal_weight": 0.0, "summary": None, "matched_entities": 0}]
    assert kinds(g) == ["list"]
    assert g.driver.calls[0]["params"] == {"namespace": "ns", "user_id": "", "limit": 1}
    assert audit.events[0]["detail"] == {"entity_names": None, "result_count": 1}


def test_find_communities_can_include_member_names():
    row = {"community_id": "k1", "member_count": 3, "internal_weight": 6.0, "summary": "S", "matched_entities": 1}
    script = [("AS matched_entities", [row]), ("AS entity_name", [{"entity_name": "Acme Corp"}, {"entity_name": "Globex Corp"}])]
    g, _ = make_index(script)
    result = g.find_communities(["Acme Corp"], namespace="ns", user_id="u1", include_members=True)
    assert kinds(g) == ["by_entity", "member_names"]
    assert result[0]["members"] == ["Acme Corp", "Globex Corp"]
    assert g.driver.calls[1]["params"] == {"community_id": "k1", "user_id": "u1", "limit": 25}


def test_find_communities_with_only_unusable_names_asks_nothing():
    g, _ = make_index([])
    assert g.find_communities(["ab", ""]) == []
    assert g.driver.calls == []
