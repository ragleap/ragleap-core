"""Offline layer: every tenant-scoped read must send its own namespace and user_id."""
import pytest
from ragleap_graph import GraphConfig, GraphIndex


class _Result(list):
    def single(self):
        return None

    def consume(self):
        return None


class _Session:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **params):
        self.log.append((query, params))
        return _Result()


class _Driver:
    def __init__(self, log):
        self.log = log

    def session(self):
        return _Session(self.log)


def _index():
    g = GraphIndex(GraphConfig(uri="bolt://127.0.0.1:1", user="neo4j", password="example-password"))
    log = []
    g.driver = _Driver(log)
    return g, log


READS = {
    "find_documents_by_entities": lambda g, **s: g.find_documents_by_entities(["Acme Corp"], **s),
    "search_related_entities": lambda g, **s: g.search_related_entities(["Acme Corp"], max_depth=3, **s),
    "find_relations_out": lambda g, **s: g.find_relations("Acme Corp", direction="outgoing", **s),
    "find_relations_in": lambda g, **s: g.find_relations("Acme Corp", direction="incoming", **s),
    "find_relations_both": lambda g, **s: g.find_relations("Acme Corp", direction="both", **s),
    "document_entities": lambda g, **s: g.document_entities("doc-1", **s),
    "find_entities_by_type": lambda g, **s: g.find_entities_by_type("ORG", **s),
    "find_lineage": lambda g, **s: g.find_lineage("Acme Corp", "Initech", **s),
    "detect_communities": lambda g, **s: g.detect_communities(**s),
    "find_communities": lambda g, **s: g.find_communities(["Acme Corp"], **s),
}


@pytest.mark.parametrize("name", sorted(READS))
def test_read_sends_the_scope_it_was_given(name):
    g, log = _index()
    READS[name](g, namespace="ns_x", user_id="u_x")
    assert log, name + " ran no statement"
    first = log[0][1]
    assert first.get("namespace") == "ns_x" and first.get("user_id") == "u_x", (name, first)


@pytest.mark.parametrize("name", sorted(READS))
def test_two_scopes_send_different_parameters(name):
    g1, l1 = _index()
    g2, l2 = _index()
    READS[name](g1, namespace="ns_x", user_id="u_x")
    READS[name](g2, namespace="ns_y", user_id="u_y")
    assert l1[0][1] != l2[0][1]


@pytest.mark.parametrize("name", sorted(READS))
def test_default_scope_is_empty_strings(name):
    g, log = _index()
    READS[name](g)
    first = log[0][1]
    assert first.get("namespace") == "" and first.get("user_id") == "", (name, first)
