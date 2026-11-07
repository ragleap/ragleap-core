"""Temporal relation edges (GraphIndex(temporal=True)) against a fake session: exact statements and parameters."""
from datetime import datetime, timezone

import pytest

from ragleap_graph import GraphConfig, GraphIndex

AWARE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _Result(list):
    def single(self):
        return None

    def consume(self):
        return None


class _Session:
    def __init__(self, log, old_rows):
        self.log, self.old_rows = log, old_rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.log.append((query, params))
        if "RETURN rw.subject AS subject" in query:
            return _Result(self.old_rows)
        return _Result()


class _Driver:
    def __init__(self, log, old_rows=()):
        self.log, self.old_rows = log, list(old_rows)

    def session(self):
        return _Session(self.log, self.old_rows)


def _index(temporal, old_rows=()):
    g = GraphIndex(GraphConfig(uri="bolt://127.0.0.1:1", user="neo4j", password="example-password"), temporal=temporal)
    log = []
    g.driver = _Driver(log, old_rows)
    return g, log


OLD = [{"subject": "acme corp", "relation_type": "PARTNERED_WITH", "object": "globex corp"}]


def _refresh_statements(log):
    return [q for q, _ in log if "MERGE (es)-[r" in q]


def test_default_write_keeps_the_delete_statement_and_has_no_time_properties():
    g, log = _index(False, OLD)
    g.upsert_document("d1", "Doc", [{"text": "Nothing to see here."}], namespace="ns")
    stmts = _refresh_statements(log)
    assert len(stmts) == 1
    assert "DELETE r2" in stmts[0] and "valid_" not in stmts[0]


def test_temporal_write_closes_instead_of_deleting():
    g, log = _index(True, OLD)
    g.upsert_document("d1", "Doc", [{"text": "Nothing to see here."}], namespace="ns")
    stmts = _refresh_statements(log)
    assert len(stmts) == 1
    assert stmts[0].strip() == GraphIndex._RELATES_AS_REFRESH_TEMPORAL.strip()
    assert "DELETE r2" not in stmts[0]
    assert "valid_from = datetime()" in stmts[0] and "SET r2.valid_to = coalesce(r2.valid_to, datetime())" in stmts[0]


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
def test_find_relations_default_is_unchanged(direction):
    g, log = _index(False)
    g.find_relations("Acme Corp", direction=direction, namespace="ns")
    query, params = log[0]
    assert "valid_" not in query and "$as_of" not in query
    assert set(params) == {"subject", "namespace", "user_id", "relation_type", "limit"}


@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
def test_find_relations_temporal_reads_only_open_edges(direction):
    g, log = _index(True)
    g.find_relations("Acme Corp", direction=direction, namespace="ns")
    query, params = log[0]
    assert query.count("r.valid_to IS NULL") == 1 and "$as_of" not in query
    marker = "WITH DISTINCT" if direction == "both" else "RETURN s.display_name"
    assert query.index("r.valid_to IS NULL") < query.index(marker)
    assert query.index("coalesce(") < query.index("r.valid_to IS NULL")
    assert set(params) == {"subject", "namespace", "user_id", "relation_type", "limit"}


@pytest.mark.parametrize("temporal", [False, True])
@pytest.mark.parametrize("direction", ["outgoing", "incoming", "both"])
def test_find_relations_as_of_filters_by_interval(direction, temporal):
    g, log = _index(temporal)
    g.find_relations("Acme Corp", direction=direction, namespace="ns", as_of=AWARE)
    query, params = log[0]
    assert "r.valid_from <= $as_of" in query and "r.valid_to > $as_of" in query
    assert "r.valid_to IS NULL\n" not in query.replace("r.valid_to IS NULL OR", "")
    marker = "WITH DISTINCT" if direction == "both" else "RETURN s.display_name"
    assert query.index("$as_of") < query.index(marker)
    assert params["as_of"] == AWARE
    assert set(params) == {"subject", "namespace", "user_id", "relation_type", "limit", "as_of"}


@pytest.mark.parametrize("bad", [datetime(2026, 1, 1), "2026-01-01", 5])
def test_as_of_must_be_timezone_aware(bad):
    g, log = _index(False)
    with pytest.raises(ValueError, match="as_of must be a timezone-aware datetime"):
        g.find_relations("Acme Corp", as_of=bad)
    assert log == []


def test_direction_is_checked_before_as_of():
    g, _ = _index(False)
    with pytest.raises(ValueError, match="direction must be"):
        g.find_relations("Acme Corp", direction="sideways", as_of="bad")


def test_temporal_must_be_a_bool():
    with pytest.raises(ValueError, match="temporal must be a bool"):
        GraphIndex(GraphConfig(uri="bolt://127.0.0.1:1", user="neo4j", password="example-password"), temporal="yes")


def test_default_community_statements_are_unchanged_and_open_variants_add_one_line():
    assert "valid_" not in GraphIndex._COMMUNITY_RELATES_EDGES and "valid_" not in GraphIndex._COMMUNITY_RELATIONS
    for default, open_ in ((GraphIndex._COMMUNITY_RELATES_EDGES, GraphIndex._COMMUNITY_RELATES_EDGES_OPEN),
                           (GraphIndex._COMMUNITY_RELATIONS, GraphIndex._COMMUNITY_RELATIONS_OPEN)):
        assert open_.count("AND r.valid_to IS NULL") == 1
        assert open_.replace("          AND r.valid_to IS NULL\n", "") == default


@pytest.mark.parametrize("temporal", [False, True])
def test_community_edge_fetch_picks_the_open_variant_only_when_temporal(temporal):
    g, log = _index(temporal)
    session = _Session(log, [])
    g._fetch_community_edges(session, "ns", "", True, 10)
    expected = GraphIndex._COMMUNITY_RELATES_EDGES_OPEN if temporal else GraphIndex._COMMUNITY_RELATES_EDGES
    assert [q for q, _ in log] == [GraphIndex._COMMUNITY_CO_OCCURS_EDGES, expected]


@pytest.mark.parametrize("temporal", [False, True])
def test_community_summary_relations_pick_the_open_variant_only_when_temporal(temporal):
    g, log = _index(temporal)
    session = _Session(log, [])
    found = [{"members": ["a", "b"], "size": 2, "internal_weight": 1.0, "members_hash": "h"}]
    result = {"summarized": 0, "summary_errors": 0}
    g._summarize_communities(session, found, ["k"], {}, [("a", "b", 1.0)], "ns", "", lambda info: "s", False, 5, result)
    expected = GraphIndex._COMMUNITY_RELATIONS_OPEN if temporal else GraphIndex._COMMUNITY_RELATIONS
    assert expected in [q for q, _ in log]
    assert (GraphIndex._COMMUNITY_RELATIONS in [q for q, _ in log]) is (not temporal)


def test_temporal_defaults_to_false():
    g = GraphIndex(GraphConfig(uri="bolt://127.0.0.1:1", user="neo4j", password="example-password"))
    assert g.temporal is False
