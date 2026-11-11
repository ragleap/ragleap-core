"""Live layer: tenant A must see exactly what it would see alone, even with cross-tenant bait.

The baseline is read while only the base tenant exists, so a query that leaks cannot inflate it.
"""
import datetime
import json
import os

import pytest
from ragleap_graph import GraphConfig, GraphIndex

URI = os.environ.get("NEO4J_URI")
USER = os.environ.get("NEO4J_USER")
PWD = os.environ.get("NEO4J_PASSWORD")
pytestmark = pytest.mark.skipif(not (URI and USER and PWD), reason="No live Neo4j credentials in environment")

PREFIX = "ragleap_graph_tenant_"
BASE, A, B, UID = PREFIX + "base", PREFIX + "a", PREFIX + "b", "u1"
TEXT = "Acme Corp and Globex Corp are rivals. Acme Corp works with Initech."
EXTRA = "Umbrella Corp and Acme Corp are partners. Umbrella Corp works with Initech."
SKIP_KEYS = ("composite_key", "community_id", "hash", "valid_", "_at", "namespace", "key")
BIG = 1000
P = dict(na=A, nb=B, ns=A, uid=UID)
FOR_Y = "y.namespace IN [$na, $nb] AND NOT (y.namespace = $ns AND coalesce(y.user_id, '') = $uid)"
FOR_D = FOR_Y.replace("y.", "d.")
A_X = "x.namespace = $ns AND coalesce(x.user_id, '') = $uid"
TENANTS = ((BASE, UID), (A, UID), (A, "u2"), (B, UID), (B, "u2"))
FOREIGN_WORDS = ("umbrella", "rel gamma", "sued_by", "bait", "leaked")


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if not any(s in k.lower() for s in SKIP_KEYS)}
    if isinstance(x, (list, tuple)):
        return sorted((_clean(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return x


def _norm(x):
    return json.dumps(_clean(x), sort_keys=True, default=str)


def _run(g, q, **p):
    with g.driver.session() as s:
        return [r.data() for r in s.run(q, **p)]


def _seed(g, ns, uid, extra=False):
    g.upsert_document("doc-1", "Acme news", [{"text": TEXT}], namespace=ns, user_id=uid)
    if extra:
        g.upsert_document("doc-2", "Umbrella news", [{"text": EXTRA}], namespace=ns, user_id=uid)
    for n in ["rel alpha", "rel beta"] + (["rel gamma"] if extra else []):
        _run(g, "CREATE (:Entity {composite_key: $k, name: $n, display_name: $d, namespace: $ns, "
                "user_id: $uid, entity_type: 'ORG'})", k=ns + "|" + uid + "|" + n, n=n, d=n.title(), ns=ns, uid=uid)
    _run(g, "CREATE (:PairWeight {composite_key: $k, namespace: $ns, document_id: 'd1', entity_a: 'rel alpha', "
            "entity_b: 'rel beta', user_id: $uid, weight: 1.0})", k="pw|" + ns + "|" + uid, ns=ns, uid=uid)
    for obj, rt in [("rel beta", "PARTNERED_WITH")] + ([("rel gamma", "SUED_BY")] if extra else []):
        _run(g, "CREATE (:RelationWeight {composite_key: $k, namespace: $ns, document_id: 'd1', "
                "subject: 'rel alpha', relation_type: $rt, object: $o, user_id: $uid, weight: 2.0})",
             k="rw|" + ns + "|" + uid + "|" + obj, ns=ns, rt=rt, o=obj, uid=uid)
        _run(g, GraphIndex._RELATES_AS_REFRESH_TEMPORAL, namespace=ns, subject="rel alpha", relation_type=rt,
             object=obj, user_id=uid, relates_as_composite_key="edge|" + ns + "|" + uid + "|" + obj)


def _alone(g, fn, refresh=True):
    """Seed ONLY the base tenant, refresh its communities, read. Nothing else exists yet."""
    _seed(g, BASE, UID)
    if refresh:
        g.refresh_communities(namespace=BASE, user_id=UID)
    return fn()


def _others(g):
    for ns, uid, extra in ((A, UID, False), (A, "u2", True), (B, UID, True), (B, "u2", True)):
        _seed(g, ns, uid, extra)


def _world(g):
    _seed(g, BASE, UID)
    _others(g)


def _refresh_all(g):
    for ns, uid in TENANTS:
        g.refresh_communities(namespace=ns, user_id=uid)


def _mark_names(g):
    _run(g, "MATCH (y:Entity) WHERE " + FOR_Y + " AND y.name IN ['acme corp', 'globex corp', 'initech'] "
            "SET y.display_name = 'LEAKED ' + y.name", **P)


def _bait(g):
    for tag, pat in (("bo", "(x)-[r:RELATES_AS {composite_key: 'bo|' + x.composite_key + '|' + y.composite_key}]->(y)"),
                     ("bi", "(y)-[r:RELATES_AS {composite_key: 'bi|' + x.composite_key + '|' + y.composite_key}]->(x)")):
        _run(g, "MATCH (x:Entity) WHERE " + A_X + " MATCH (y:Entity) WHERE " + FOR_Y + " MERGE " + pat +
                " ON CREATE SET r.relation_type = 'BAIT', r.weight = 1.0, r.namespace = $ns, r.user_id = $uid", **P)
    _run(g, "MATCH (x:Entity) WHERE " + A_X + " MATCH (y:Entity) WHERE " + FOR_Y +
            " MERGE (x)-[r:CO_OCCURS_WITH {composite_key: 'bc|' + x.composite_key + '|' + y.composite_key}]->(y)"
            " ON CREATE SET r.weight = 1.0, r.namespace = $ns, r.user_id = $uid", **P)
    _run(g, "MATCH (d:Document) WHERE " + FOR_D + " MATCH (x:Entity) WHERE " + A_X + " MERGE (d)-[:CONTAINS]->(x)", **P)
    _run(g, "MATCH (d:Document) WHERE d.namespace = $ns AND coalesce(d.user_id, '') = $uid "
            "MATCH (y:Entity) WHERE " + FOR_Y + " MERGE (d)-[:CONTAINS]->(y)", **P)


def _bait_membership(g, who):
    _run(g, "MATCH (c:Community) WHERE c.namespace = $ns AND coalesce(c.user_id, '') = $uid "
            "MATCH (y:Entity {name: 'acme corp'}) WHERE " + who + " MERGE (y)-[:IN_COMMUNITY]->(c)", **P)


def _reads(g, ns, uid, search=False):
    s = dict(namespace=ns, user_id=uid)
    now = datetime.datetime.now(datetime.timezone.utc)
    out = {
        "find_documents_by_entities": g.find_documents_by_entities(["Acme Corp", "Rel Alpha"], limit=BIG, **s),
        "document_entities": g.document_entities("doc-1", **s),
        "find_entities_by_type": g.find_entities_by_type("ORG", limit=BIG, **s),
        "find_lineage": g.find_lineage("Rel Alpha", "Rel Beta", limit=BIG, **s),
        "find_lineage_gamma": g.find_lineage("Rel Alpha", "Rel Gamma", limit=BIG, **s),
        "find_communities": g.find_communities(["Acme Corp"], include_members=True, limit=BIG, **s),
        "find_communities_list": g.find_communities(None, limit=BIG, **s),
        "detect_communities": g.detect_communities(**s),
    }
    if search:
        for d in (1, 2, 3):
            out["search_related_%d" % d] = g.search_related_entities(["Acme Corp", "Rel Alpha"], max_depth=d, limit=BIG, **s)
    for direction in ("outgoing", "incoming", "both"):
        for ent in ("Rel Alpha", "Rel Beta"):
            out["relations_%s_%s" % (direction, ent)] = g.find_relations(ent, direction=direction, limit=BIG, **s)
            out["relations_asof_%s_%s" % (direction, ent)] = g.find_relations(ent, direction=direction, as_of=now, limit=BIG, **s)
    return out


def _wipe(g):
    _run(g, "MATCH (n) WHERE n.namespace STARTS WITH $p DETACH DELETE n", p=PREFIX)


@pytest.fixture()
def g():
    gi = GraphIndex(GraphConfig(uri=URI, user=USER, password=PWD), temporal=True)
    assert gi.driver is not None
    _wipe(gi)
    yield gi
    _wipe(gi)
    gi.close()


def _compare(base, crowded, names=None):
    problems = []
    for name in sorted(names or base):
        if _norm(base[name]) != _norm(crowded[name]):
            problems.append(name)
        low = json.dumps(crowded[name], default=str).lower()
        if any(w in low for w in FOREIGN_WORDS):
            problems.append(name + " (contains data of another tenant)")
    return problems


def test_tenant_a_sees_only_what_it_would_see_alone(g):
    base = _alone(g, lambda: _reads(g, BASE, UID))
    _others(g)
    _bait(g)
    _mark_names(g)
    _refresh_all(g)
    crowded = _reads(g, A, UID)
    for must_have in ("find_entities_by_type", "relations_outgoing_Rel Alpha", "relations_incoming_Rel Beta",
                      "find_lineage", "find_communities", "find_communities_list", "document_entities"):
        assert len(base[must_have]) > 0, "baseline empty, test would pass trivially: " + must_have
    print("baseline rows per read:", {k: len(v) for k, v in base.items()})
    assert not _compare(base, crowded)


def test_search_related_ignores_other_tenants(g):
    base = _alone(g, lambda: _reads(g, BASE, UID, True), refresh=False)
    _others(g)
    _run(g, "MATCH (x:Entity {name: 'rel alpha'}) WHERE " + A_X + " MATCH (y:Entity {name: 'rel gamma'}) WHERE " + FOR_Y +
            " MERGE (x)-[:BAIT_LINK]->(y)", **P)
    crowded = _reads(g, A, UID, True)
    names = [k for k in base if k.startswith("search_related")]
    assert len(base["search_related_3"]) > 0
    assert not _compare(base, crowded, names)


def test_search_related_does_not_use_other_tenants_as_shortcut(g):
    base = _alone(g, lambda: _reads(g, BASE, UID, True), refresh=False)
    _others(g)
    _run(g, "MATCH (x:Entity {name: 'acme corp'}) WHERE " + A_X + " MATCH (y:Entity {name: 'rel gamma'}) WHERE " + FOR_Y +
            " MERGE (x)-[:BAIT_LINK]->(y)", **P)
    _run(g, "MATCH (x:Entity {name: 'rel beta'}) WHERE " + A_X + " MATCH (y:Entity {name: 'rel gamma'}) WHERE " + FOR_Y +
            " MERGE (y)-[:BAIT_LINK]->(x)", **P)
    crowded = _reads(g, A, UID, True)
    names = [k for k in base if k.startswith("search_related")]
    assert not _compare(base, crowded, names)


def test_refresh_for_a_leaves_other_tenants_communities_untouched(g):
    for ns, uid in ((A, UID), (A, "u2"), (B, UID), (B, "u2")):
        _seed(g, ns, uid, extra=True)
        g.refresh_communities(namespace=ns, user_id=uid)
    others = [(A, "u2"), (B, UID), (B, "u2")]

    def snap():
        rows = _run(g, "MATCH (c:Community) WHERE c.namespace STARTS WITH $p "
                       "OPTIONAL MATCH (e)-[r:IN_COMMUNITY]->(c) "
                       "RETURN c.namespace AS ns, coalesce(c.user_id,'') AS uid, count(DISTINCT c) AS c, count(r) AS r", p=PREFIX)
        return sorted((x["ns"], x["uid"], x["c"], x["r"]) for x in rows)

    before = [t for t in snap() if (t[0], t[1]) in others]
    g.refresh_communities(namespace=A, user_id=UID, force=True)
    after = [t for t in snap() if (t[0], t[1]) in others]
    assert before == after and len(before) == 3


def test_refresh_creates_no_cross_tenant_membership(g):
    _world(g)
    _refresh_all(g)
    rows = _run(g, "MATCH (e:Entity)-[:IN_COMMUNITY]->(c:Community) WHERE e.namespace STARTS WITH $p "
                   "RETURN count(*) AS total, sum(CASE WHEN e.namespace <> c.namespace OR coalesce(e.user_id,'') <> "
                   "coalesce(c.user_id,'') THEN 1 ELSE 0 END) AS crossing", p=PREFIX)
    assert rows[0]["total"] > 0 and rows[0]["crossing"] == 0, rows


def test_community_lookup_ignores_foreign_membership(g):
    s_b, s_a = dict(namespace=BASE, user_id=UID), dict(namespace=A, user_id=UID)
    base = _alone(g, lambda: {"by_entity": g.find_communities(["Acme Corp"], limit=BIG, **s_b),
                              "list": g.find_communities(None, limit=BIG, **s_b)})
    _others(g)
    _refresh_all(g)
    _bait_membership(g, FOR_Y)
    _run(g, "MATCH (x:Entity {name: 'acme corp'}) WHERE " + A_X + " MATCH (c:Community) WHERE c.namespace IN [$na, $nb] "
            "AND NOT (c.namespace = $ns AND coalesce(c.user_id, '') = $uid) MERGE (x)-[:IN_COMMUNITY]->(c)", **P)
    crowded = {"by_entity": g.find_communities(["Acme Corp"], limit=BIG, **s_a), "list": g.find_communities(None, limit=BIG, **s_a)}
    assert base["by_entity"] and base["list"] and not _compare(base, crowded)


def test_member_lookup_ignores_same_namespace_other_user(g):
    base = _alone(g, lambda: {"m": g.find_communities(["Acme Corp"], include_members=True, namespace=BASE, user_id=UID)})
    _others(g)
    _refresh_all(g)
    _bait_membership(g, "y.namespace = $ns AND coalesce(y.user_id, '') <> $uid")
    crowded = {"m": g.find_communities(["Acme Corp"], include_members=True, namespace=A, user_id=UID)}
    assert base["m"] and not _compare(base, crowded)


def test_member_lookup_ignores_cross_namespace_members(g):
    base = _alone(g, lambda: {"m": g.find_communities(["Acme Corp"], include_members=True, namespace=BASE, user_id=UID)})
    _others(g)
    _refresh_all(g)
    _bait_membership(g, "y.namespace = $nb AND coalesce(y.user_id, '') = $uid")
    crowded = {"m": g.find_communities(["Acme Corp"], include_members=True, namespace=A, user_id=UID)}
    assert base["m"] and not _compare(base, crowded)


def test_summarizer_input_contains_only_own_tenant(g):
    def run(ns):
        box = []

        def summ(info):
            box.append(info)
            return "summary"
        g.refresh_communities(namespace=ns, user_id=UID, include_relations=True, summarizer=summ, force=True)
        return box

    base = _alone(g, lambda: run(BASE))
    _others(g)
    _bait(g)
    _mark_names(g)
    crowded = run(A)
    assert base, "summarizer never called"
    assert _norm(base) == _norm(crowded)
    assert not any(w in json.dumps(crowded, default=str).lower() for w in FOREIGN_WORDS)
