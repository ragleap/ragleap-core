"""Interop with the official MCP Python SDK client (v1 API: pip install "mcp>=1.28,<2"). Skipped if absent."""
import asyncio
import json
import threading

import pytest

from ragleap_physical import PhysicalTools
from ragleap_physical.mcp_server import make_server

from helpers import make
from test_tools import args


def test_official_sdk_client_can_list_and_call():
    pytest.importorskip("mcp")
    from mcp import ClientSession
    try:  # newer SDKs renamed the function and deprecate the old name
        from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client
    b, adapter, _ = make()
    server = make_server(PhysicalTools(b))
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def run():
        async with streamablehttp_client(url) as streams:
            read, write = streams[0], streams[1]  # newer SDKs return two values, older ones three
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [t.name for t in listed.tools]
                res = await session.call_tool("physical_propose_action", args())
                return names, res

    try:
        names, res = asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
    assert names == ["physical_list_sources", "physical_get_observation", "physical_get_envelope",
                     "physical_propose_action"]
    is_error = res.is_error if hasattr(res, "is_error") else res.isError  # renamed in mcp 2.x
    assert is_error is False
    assert json.loads(res.content[0].text)["data"]["status"] == "validated"
    assert adapter.applied == []
