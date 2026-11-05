"""
examples/01_ingest_and_query.py must work against an installed setup, where the API needs a key:
it sends the key from RAGLEAP_API_KEY, sends nothing when it is not set, and stops with a clear message on a 401.
"""
import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "01_ingest_and_query.py")
ENV_NAME = "RAGLEAP_" + "API_KEY"


def load(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, value)
    monkeypatch.delenv("RAGLEAP_URL", raising=False)
    spec = importlib.util.spec_from_file_location("example01", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reply(status=200, body=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body or {}
    response.raise_for_status.side_effect = None if status < 400 else RuntimeError("http error")
    return response


def test_the_key_is_sent_on_upload_and_chat(monkeypatch, tmp_path):
    module = load(monkeypatch, "example-value")
    sample = tmp_path / "sample.txt"
    sample.write_text("hello")
    with patch.object(module.requests, "post", return_value=reply(body={"chunks_stored": 1, "document_id": "d"})) as post:
        module.ingest_document(str(sample))
        assert post.call_args.kwargs["headers"] == {"x-api-key": "example-value"}
        module.ask_question("q?")
        assert post.call_args.kwargs["headers"] == {"x-api-key": "example-value"}


def test_no_header_is_sent_when_no_key_is_set(monkeypatch):
    module = load(monkeypatch, None)
    with patch.object(module.requests, "post", return_value=reply(body={"answer": "a"})) as post:
        module.ask_question("q?")
        assert post.call_args.kwargs["headers"] == {}


def test_a_401_stops_with_a_clear_message(monkeypatch):
    module = load(monkeypatch, None)
    with patch.object(module.requests, "post", return_value=reply(status=401)):
        with pytest.raises(SystemExit) as stop:
            module.ask_question("q?")
    assert "API key" in str(stop.value) and "RAGLEAP_" in str(stop.value)


def test_the_server_address_can_be_changed(monkeypatch):
    monkeypatch.setenv("RAGLEAP_URL", "http://example.invalid:9000/")
    monkeypatch.delenv(ENV_NAME, raising=False)
    spec = importlib.util.spec_from_file_location("example01b", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.API_URL == "http://example.invalid:9000"
