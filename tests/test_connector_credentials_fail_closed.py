"""Connector credentials are never stored in plain text, and the Mongo filter is data, not spliced text (AMQIC/CERNOTIS S1-001, S1-006)."""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from cryptography.fernet import Fernet

from core.integrations import service
from core.integrations.base import (DataSource, MissingEncryptionKey, protect_headers, reveal_headers)
from core.integrations.mongodb import MongoDBConnector


class _Cur:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append(params)

    def close(self):
        pass


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def stored(monkeypatch):
    log = []
    monkeypatch.setattr(service, "_get_connection", lambda: _Conn(log))
    return log


def _no_key(monkeypatch):
    monkeypatch.delenv("ADDON_ENCRYPTION_KEY", raising=False)


def _key(monkeypatch):
    monkeypatch.setenv("ADDON_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_the_error_is_a_value_error_so_the_route_answers_400():
    assert issubclass(MissingEncryptionKey, ValueError)


@pytest.mark.parametrize("field,value", [
    ("connection_string", "mongodb://user:pw@host/db"),
    ("api_key", "sk-secret-value"),
    ("api_headers", {"Authorization": "Bearer abc"}),
    ("api_headers", {"X-Api-Key": "abc"}),
])
def test_without_a_key_a_credential_is_refused_and_nothing_is_stored(monkeypatch, stored, field, value):
    _no_key(monkeypatch)
    with pytest.raises(MissingEncryptionKey):
        service.create_data_source("s", "rest_api", **{field: value})
    assert stored == []


def test_without_a_key_a_source_with_no_credentials_still_works(monkeypatch, stored):
    _no_key(monkeypatch)
    service.create_data_source("s", "rest_api", api_endpoint="https://example.org/data", api_headers={"Accept": "application/json"})
    assert len(stored) == 1
    assert json.loads(stored[0][6]) == {"Accept": "application/json"}


def test_with_a_key_credentials_are_stored_encrypted_and_round_trip(monkeypatch, stored):
    _key(monkeypatch)
    headers = {"Authorization": "Bearer abc", "Accept": "application/json"}
    service.create_data_source("s", "rest_api", connection_string="mongodb://user:pw@host/db", api_key="sk-secret-value", api_headers=headers)
    row = stored[0]
    assert "pw@host" not in str(row) and "sk-secret-value" not in str(row) and "Bearer abc" not in str(row)
    assert set(json.loads(row[6])) == {"__enc__"}
    assert reveal_headers(json.loads(row[6])) == headers
    ds = service._row_to_data_source(
        ("id1", "s", "rest_api", row[3], row[4], row[5], json.loads(row[6]), None, {}, "user_id", "documents", None, None))
    assert ds.connection_string == "mongodb://user:pw@host/db"
    assert ds.api_key == "sk-secret-value"
    assert ds.api_headers == headers


def test_older_plain_header_rows_still_read(monkeypatch):
    _key(monkeypatch)
    assert reveal_headers({"Authorization": "Bearer old"}) == {"Authorization": "Bearer old"}
    assert reveal_headers(None) == {}
    assert protect_headers({"Accept": "x"}) == {"Accept": "x"}


class _Coll:
    def __init__(self, seen):
        self.seen = seen

    def find(self, flt, projection=None):
        self.seen.append(flt)
        return []


class _Db:
    def __init__(self, seen):
        self.seen = seen

    def __getitem__(self, name):
        return _Coll(self.seen)


class _Client:
    seen = []

    def __init__(self, *a, **k):
        pass

    def __getitem__(self, name):
        return _Db(_Client.seen)

    def close(self):
        pass


@pytest.fixture
def mongo(monkeypatch):
    _Client.seen = []
    monkeypatch.setitem(sys.modules, "pymongo", types.SimpleNamespace(MongoClient=_Client))
    monkeypatch.setitem(sys.modules, "bson", types.SimpleNamespace(ObjectId=type("ObjectId", (), {})))

    def run(flt, identifier):
        ds = DataSource(id="1", name="m", source_type="mongodb", connection_string="mongodb://x",
                        query_template=json.dumps({"database": "d", "collection": "c", "filter": flt}))
        MongoDBConnector(ds).fetch_data(identifier)
        return _Client.seen[-1]
    return run


def test_the_identifier_is_a_plain_value_in_the_filter(mongo):
    assert mongo({"user_id": "{{user_id}}"}, "alice") == {"user_id": "alice"}


def test_a_quote_in_the_identifier_cannot_add_keys_or_operators(mongo):
    evil = 'a", "role": {"$ne": "x'
    got = mongo({"user_id": "{{user_id}}"}, evil)
    assert set(got) == {"user_id"} and got["user_id"] == evil


def test_placeholders_in_nested_filters_are_filled(mongo):
    got = mongo({"$or": [{"owner": "{{user_identifier}}"}, {"cc": "x-{{user_id}}"}]}, "bob")
    assert got == {"$or": [{"owner": "bob"}, {"cc": "x-bob"}]}
